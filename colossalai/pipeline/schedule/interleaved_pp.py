from functools import partial
from typing import Any, Callable, Iterable, List, Optional, Union

import torch
import torch.cuda
from torch.nn import Module, ModuleList
from torch.utils._pytree import tree_map

from colossalai.interface import OptimizerWrapper
from colossalai.pipeline.p2p import PipelineP2PCommunication, create_fast_send_metadata
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.utils.device import get_current_device

from ._utils import detach, get_batch_size, get_micro_batch, merge_batch, model_forward, retain_grad, to_device
from .base import PipelineSchedule


class InterleavedSchedule(PipelineSchedule):
    def __init__(
        self,
        stage_manager: PipelineStageManager,
        num_model_chunks: int,
        num_microbatch: Optional[int] = None,
        microbatch_size: Optional[int] = None,
    ) -> None:
        super().__init__(stage_manager)
        assert (
            num_microbatch is not None or microbatch_size is not None
        ), "Either num_microbatch or microbatch_size should be provided"

        self.comm = PipelineP2PCommunication(stage_manager)
        self.num_microbatch = num_microbatch
        self.microbatch_size = microbatch_size
        self.num_model_chunks = num_model_chunks

        self.batch: Any
        self.batch_size: int
        self.last_batch_size: Optional[int] = None
        self.microbatch_offset: List[int]

        # P2PMeta cache
        self.send_metadata_forward = True
        self.send_metadata_backward = True
        self.metadata_recv_forward = None
        self.metadata_recv_backward = None

    def load_batch(self, data_iter: Iterable, device: Optional[torch.device] = None) -> None:
        """Load a batch from data iterator.

        Args:
            data_iter (Iterable): Data iterator.
            device (Optional[torch.device], optional): Target device. Defaults to None.
        """
        batch = next(data_iter)
        if device is not None:
            batch = tree_map(partial(to_device, device=device), batch)
        self.batch = batch
        self.batch_size = get_batch_size(batch)
        if self.last_batch_size is None:
            self.last_batch_size = self.batch_size
        else:
            assert self.forward_only or self.last_batch_size == self.batch_size
            # TODO: support arbitrary batch size when forward_only=True
        self.microbatch_offset = [0 for _ in range(self.num_model_chunks)]
        if self.num_microbatch is not None:
            assert self.batch_size % self.num_microbatch == 0, "Batch size should divided by the number of microbatch"
            self.microbatch_size = self.batch_size // self.num_microbatch
        elif self.microbatch_size is not None:
            assert self.batch_size % self.microbatch_size == 0, "Batch size should divided by the microbatch size"
            self.num_microbatch = self.batch_size // self.microbatch_size
        else:
            raise ValueError("Either num_microbatch or microbatch_size should be provided")

        assert (
            self.num_microbatch % self.num_model_chunks == 0
        ), "Number of microbatch should be an integer multiple of number of model chunks"

        assert (
            self.num_microbatch % self.stage_manager.num_stages == 0
        ), "Number of microbatch should be an integer multiple of number of pipeline parallel devices"

    def load_micro_batch(self, model_chunk_id: int) -> Any:
        """Load a micro batch from the current batch.

        Args:
            microbatch_id (int): the current model chunk idx.

        Returns:
            Any: Micro batch.
        """
        micro_batch = get_micro_batch(self.batch, self.microbatch_offset[model_chunk_id], self.microbatch_size)
        self.microbatch_offset[model_chunk_id] += self.microbatch_size
        return tree_map(partial(to_device, device=get_current_device()), micro_batch)

    def get_model_chunk_id(self, microbatch_id: int, is_forward: bool) -> int:
        """Helper method to get the model chunk ID given the iteration number.

        Args:
            microbatch_id (int): the current microbatch idx
            forward (bool): if is the forward process

        Returns:
            int: The model chunk idx of the input microbatch_id
        """
        microbatch_id_in_group = (microbatch_id) % (self.stage_manager.num_stages * self.num_model_chunks)
        model_chunk_id = microbatch_id_in_group // self.stage_manager.num_stages
        if not is_forward:
            model_chunk_id = self.num_model_chunks - model_chunk_id - 1
        return model_chunk_id

    def recv_forward(self, model_chunk_id: int, prev_rank: int = None) -> Any:
        """Copy the forward output from the previous stage in pipeline as the input tensor of this stage.
           For interleaved 1F1B.

        Args:
            model_chunk_id (int): The current model chunk idx.
            prev_rank (int, optional): The rank of the source of the tensor.

        Returns:
            Any: The input tensor or input tensor list.
        """
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_first_stage():
                input_tensor = self.comm.recv_forward(prev_rank, metadata_recv=self.metadata_recv_forward)
                if self.metadata_recv_forward is None:
                    self.metadata_recv_forward = create_fast_send_metadata(input_tensor)

                return input_tensor

    def recv_backward(self, model_chunk_id: int, next_rank: int = None) -> Any:
        """Copy the gradient tensor from the next stage in pipeline as the input gradient of this stage.
           For interleaved 1F1B.

        Args:
            model_chunk_id (int): The current model chunk idx.
            next_rank (int, optional): The rank of the source of the tensor.

        Returns:
            Any: The input gradient tensor or gradient tensor list.
        """
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_last_stage():
                output_tensor_grad = self.comm.recv_backward(next_rank, metadata_recv=self.metadata_recv_backward)
                if self.metadata_recv_backward is None:
                    self.metadata_recv_backward = create_fast_send_metadata(output_tensor_grad)

                return output_tensor_grad

    def send_forward(self, model_chunk_id: int, output_object: Any, next_rank: int = None) -> None:
        """Sends the input tensor to the next stage in pipeline.
           For interleaved 1F1B.

        Args:
            model_chunk_id (int): The current model chunk idx.
            output_object (Any): Object to be sent.
            next_rank (int, optional): The rank of the recipient of the tensor.
        """
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_last_stage():
                self.comm.send_forward(output_object, next_rank, send_metadata=self.send_metadata_forward)
                self.send_metadata_forward = False

    def send_backward(self, model_chunk_id: int, input_object: Any, prev_rank: int = None) -> None:
        """Sends the gradient tensor to the previous stage in pipeline.
           For interleaved 1F1B.

        Args:
            model_chunk_id (int): The current model chunk idx.
            input_object (Any): Object to be sent.
            prev_rank (int, optional): The rank of the recipient of the tensor
        """
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_first_stage():
                self.comm.send_backward(input_object, prev_rank, send_metadata=self.send_metadata_backward)
                self.send_metadata_backward = False

    def send_forward_recv_backward(
        self, model_chunk_id: int, output_object: Any, next_rank: Optional[int] = None
    ) -> Any:
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_last_stage():
                output_tensor_grad = self.comm.send_forward_recv_backward(
                    output_object,
                    next_rank,
                    send_metadata=self.send_metadata_forward,
                    metadata_recv=self.metadata_recv_backward,
                )
                self.send_metadata_forward = False
                if self.metadata_recv_backward is None:
                    self.metadata_recv_backward = create_fast_send_metadata(output_tensor_grad)

                return output_tensor_grad

    def send_backward_recv_forward(
        self, model_chunk_id: int, output_object: Any, prev_rank: Optional[int] = None
    ) -> Any:
        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if not self.stage_manager.is_first_stage():
                input_tensor = self.comm.send_backward_recv_forward(
                    output_object,
                    prev_rank,
                    send_metadata=self.send_metadata_backward,
                    metadata_recv=self.metadata_recv_forward,
                )
                self.send_metadata_backward = False
                if self.metadata_recv_forward is None:
                    self.metadata_recv_forward = create_fast_send_metadata(input_tensor)

                return input_tensor

    def forward_step(
        self,
        model_chunk: Union[ModuleList, Module],
        model_chunk_id: int,
        input_obj: Optional[dict],
        criterion: Callable,
        accum_loss: Optional[torch.Tensor] = None,
        outputs: Optional[List[Any]] = None,
    ) -> Union[torch.Tensor, dict]:
        """Forward one step of the pipeline
        Args:
            model (ModuleList or Module): Model Chunk to be run
            input_obj (Optional[dict]): The output from the previous stage. If it is the first stage, the `input_obj` is None.
            criterion (Callable): Criterion to calculate loss.
            accum_loss (Optional[torch.Tensor], optional): Accumulated loss. Defaults to None.
            outputs (Optional[List[Any]], optional): List to store the output of the last stage (final output). Defaults to None.

        Returns:
            Union[torch.Tensor, dict]: The intermediate output (dict) of the current stage. If it is the last stage, the output is the loss (Tensor).
        """
        micro_batch = self.load_micro_batch(model_chunk_id=model_chunk_id)

        # for the first stage, input_obj is None
        # for the non-first stage, input_obj is the output of the previous stage and it's must be a dict

        with self.stage_manager.switch_model_chunk_id(model_chunk_id):
            if isinstance(model_chunk, ModuleList):
                output_obj = model_forward(model_chunk[model_chunk_id], micro_batch, input_obj)
            else:
                # NOTE: in shardformer, each device still has the entire model, so we need to use relevant stage layers
                internal_inputs = {} if input_obj is None else input_obj
                internal_inputs["stage_index"] = self.stage_manager.stage_indices[model_chunk_id]
                output_obj = model_forward(model_chunk, micro_batch, internal_inputs)

            if self.stage_manager.is_last_stage():
                loss = criterion(output_obj, micro_batch) / self.num_microbatch
                if accum_loss is not None:
                    accum_loss.add_(loss.detach())
                if outputs is not None:
                    outputs.append(tree_map(detach, output_obj))
                return loss
            else:
                return output_obj

    def backward_step(
        self,
        optimizer: OptimizerWrapper,
        input_obj: Optional[dict],
        output_obj: Union[dict, torch.Tensor],
        output_obj_grad: Optional[dict],
    ) -> Optional[dict]:
        """Backward one step of the pipeline

        Args:
            optimizer (OptimizerWrapper): Optimizer to update the model
            input_obj (Optional[dict]): Output of the previous stage. If it is the first stage, the `input_obj` is None.
            output_obj (Union[dict, torch.Tensor]): Output of the current stage. If it is the last stage, the output is the loss (Tensor).
            output_obj_grad (dict): Gradient of the `output_obj`. If it is the last stage, the `output_obj_grad` is None.

        Returns:
            Optional[dict]: Gradient of the `input_obj`. If it is the first stage, the `input_obj_grad` is None.
        """

        # Retain the grad on the input_obj.
        tree_map(retain_grad, input_obj)

        # Backward pass.
        if output_obj_grad is None:
            optimizer.backward(output_obj)
        else:
            if "backward_tensor_keys" not in output_obj:
                for k, grad in output_obj_grad.items():
                    optimizer.backward_by_grad(output_obj[k], grad)
            else:
                for k, grad in output_obj_grad.items():
                    output_obj[k].grad = grad
                for k in output_obj["backward_tensor_keys"]:
                    tensor_to_backward = output_obj[k]
                    optimizer.backward_by_grad(tensor_to_backward, tensor_to_backward.grad)

        # Collect the grad of the input_obj.
        input_obj_grad = None
        if input_obj is not None:
            input_obj_grad = {}
            for k, v in input_obj.items():
                if isinstance(v, torch.Tensor) and v.grad is not None:
                    input_obj_grad[k] = v.grad
        return input_obj_grad

    def forward_backward_step(
        self,
        model_chunk: Union[ModuleList, Module],
        data_iter: Iterable,
        criterion: Callable[..., Any],
        optimizer: Optional[OptimizerWrapper] = None,
        return_loss: bool = False,
        return_outputs: bool = False,
    ) -> dict:
        """Runs interleaved schedule, with communication between pipeline stages.

        Args:
            model_chunk (ModuleList or Module): Model Chunk to be trained. Original interleaved uses a module list whereas shardformer uses entire model + layer specification
            data_iter (Iterable): Data iterator.
            criterion (Callable[[Any, Any], Tensor]): Criterion to be used. It should take two arguments: model outputs and inputs, and returns loss tensor.
            optimizer (OptimizerWrapper, optional): Optimizer to be used. Can be None when only forward is executed. Defaults to None.
            return_loss (bool, optional): Whether to return loss. Defaults to False. Whether to return loss.
            return_outputs (bool, optional): Whether to return model outputs. Defaults to False. Whether to return model outputs.

        Returns:
            dict: A dict with keys: 'loss' and 'outputs'.
        """
        self.forward_only = not torch.is_grad_enabled()
        if optimizer is None:
            assert self.forward_only, "Optimizer should be passed when doing backward."

        self.load_batch(data_iter)

        num_microbatch = self.num_microbatch * self.num_model_chunks
        if self.forward_only:
            num_warmup_microbatch = num_microbatch
        else:
            num_warmup_microbatch = (self.stage_manager.num_stages - self.stage_manager.stage - 1) * 2
            num_warmup_microbatch += (self.num_model_chunks - 1) * self.stage_manager.num_stages
            num_warmup_microbatch = min(num_warmup_microbatch, num_microbatch)

        num_microbatch_remaining = num_microbatch - num_warmup_microbatch

        # Input, output tensors only need to be saved when doing backward passes
        input_objs = None
        output_objs = None

        if not self.forward_only:
            input_objs = [[] for _ in range(self.num_model_chunks)]
            output_objs = [[] for _ in range(self.num_model_chunks)]

        outputs = [] if return_outputs and self.stage_manager.is_last_stage(ignore_chunk=True) else None

        accum_loss = None
        if return_loss and self.stage_manager.is_last_stage(ignore_chunk=True):
            accum_loss = torch.zeros(1, device=get_current_device())

        # Run warmup forward passes.
        for i in range(num_warmup_microbatch):
            model_chunk_id = self.get_model_chunk_id(i, is_forward=True)
            input_obj = self.recv_forward(model_chunk_id)
            output_obj = self.forward_step(model_chunk, model_chunk_id, input_obj, criterion, accum_loss, outputs)
            if not self.forward_only:
                input_objs[model_chunk_id].append(input_obj)
                output_objs[model_chunk_id].append(output_obj)
            self.send_forward(model_chunk_id, output_obj)

        if num_microbatch_remaining > 0:
            model_chunk_id = self.get_model_chunk_id(num_warmup_microbatch, is_forward=True)
            input_obj = self.recv_forward(model_chunk_id)

        # Run 1F1B in steady state.
        for i in range(num_microbatch_remaining):
            model_chunk_id = self.get_model_chunk_id(i + num_warmup_microbatch, is_forward=True)
            last_iteration = i == num_microbatch_remaining - 1

            output_obj = self.forward_step(model_chunk, model_chunk_id, input_obj, criterion, accum_loss, outputs)
            if self.forward_only:
                if not last_iteration:
                    input_obj = self.send_forward_recv_backward(model_chunk_id, output_obj)
                else:
                    self.send_forward(model_chunk_id, output_obj)

            else:
                self.send_forward(model_chunk_id, output_obj)
                # Add input_obj and output_obj to end of list.
                input_objs[model_chunk_id].append(input_obj)
                output_objs[model_chunk_id].append(output_obj)

                model_chunk_id = self.get_model_chunk_id(i, is_forward=False)
                output_obj_grad = self.recv_backward(model_chunk_id)

                # Pop output_obj and output_obj from the start of the list for
                # the backward pass.
                input_obj = input_objs[model_chunk_id].pop(0)
                output_obj = output_objs[model_chunk_id].pop(0)

                # backward
                input_obj_grad = self.backward_step(optimizer, input_obj, output_obj, output_obj_grad)
                self.send_backward(model_chunk_id, input_obj_grad)

                if not last_iteration:
                    model_chunk_id = self.get_model_chunk_id(i + num_warmup_microbatch + 1, is_forward=True)
                    input_obj = self.recv_forward(model_chunk_id)

        # Run cooldown backward passes.
        if not self.forward_only:
            for i in range(num_microbatch_remaining, num_microbatch):
                model_chunk_id = self.get_model_chunk_id(i, is_forward=False)
                input_obj = input_objs[model_chunk_id].pop(0)
                output_obj = output_objs[model_chunk_id].pop(0)
                output_obj_grad = self.recv_backward(model_chunk_id)
                input_obj_grad = self.backward_step(optimizer, input_obj, output_obj, output_obj_grad)
                self.send_backward(model_chunk_id, input_obj_grad)

        if not self.forward_only:
            assert all(len(v) == 0 for v in input_objs) and all(len(v) == 0 for v in output_objs)

        if outputs is not None:
            outputs = merge_batch(outputs)
        return {"loss": accum_loss, "outputs": outputs}
