from typing import Callable, Tuple, List, Any, Union
from easydict import EasyDict

import os
import numpy as np
import torch
import torch.distributed as dist
import datetime

from .default_helper import error_wrapper

# from .slurm_helper import get_master_addr


def get_rank() -> int:
    """
    Overview:
        Get the rank of current process in total world_size
    """
    # return int(os.environ.get('SLURM_PROCID', 0))
    return error_wrapper(dist.get_rank, 0)()


def get_world_size() -> int:
    """
    Overview:
        Get the world_size(total process number in data parallel training)
    """
    # return int(os.environ.get('SLURM_NTASKS', 1))
    return error_wrapper(dist.get_world_size, 1)()


broadcast = dist.broadcast
allgather = dist.all_gather
broadcast_object_list = dist.broadcast_object_list


def allreduce(x: torch.Tensor) -> None:
    """
    Overview:
        All reduce the tensor ``x`` in the world
    Arguments:
        - x (:obj:`torch.Tensor`): the tensor to be reduced
    """

    dist.all_reduce(x)
    x.div_(get_world_size())


def allreduce_with_indicator(grad: torch.Tensor, indicator: torch.Tensor) -> None:
    """
    Overview:
        Custom allreduce: Sum both the gradient and indicator tensors across all processes.
        Then, if at least one process contributed (i.e., the summation of indicator > 0),
        divide the gradient by the summed indicator. This ensures that if only a subset of
        GPUs contributed a gradient, the averaging is performed based on the actual number
        of contributors rather than the total number of GPUs.
    Arguments:
        - grad (torch.Tensor): Local gradient tensor to be reduced.
        - indicator (torch.Tensor): A tensor flag (1 if the gradient is computed, 0 otherwise).
    """
    # Allreduce (sum) the gradient and indicator
    dist.all_reduce(grad)
    dist.all_reduce(indicator)

    # Avoid division by zero. If indicator is close to 0 (extreme case), grad remains zeros.
    if not torch.isclose(indicator, torch.tensor(0.0)):
        grad.div_(indicator.item())


def allreduce_async(name: str, x: torch.Tensor) -> None:
    """
    Overview:
        All reduce the tensor ``x`` in the world asynchronously
    Arguments:
        - name (:obj:`str`): the name of the tensor
        - x (:obj:`torch.Tensor`): the tensor to be reduced
    """

    x.div_(get_world_size())
    dist.all_reduce(x, async_op=True)


def reduce_data(x: Union[int, float, torch.Tensor], dst: int) -> Union[int, float, torch.Tensor]:
    """
    Overview:
        Reduce the tensor ``x`` to the destination process ``dst``
    Arguments:
        - x (:obj:`Union[int, float, torch.Tensor]`): the tensor to be reduced
        - dst (:obj:`int`): the destination process
    """

    if np.isscalar(x):
        x_tensor = torch.as_tensor([x]).cuda()
        dist.reduce(x_tensor, dst)
        return x_tensor.item()
    elif isinstance(x, torch.Tensor):
        dist.reduce(x, dst)
        return x
    else:
        raise TypeError("not supported type: {}".format(type(x)))


def allreduce_data(x: Union[int, float, torch.Tensor], op: str) -> Union[int, float, torch.Tensor]:
    """
    Overview:
        All reduce the tensor ``x`` in the world
    Arguments:
        - x (:obj:`Union[int, float, torch.Tensor]`): the tensor to be reduced
        - op (:obj:`str`): the operation to perform on data, support ``['sum', 'avg']``
    """

    assert op in ['sum', 'avg'], op
    if np.isscalar(x):
        x_tensor = torch.as_tensor([x]).cuda()
        dist.all_reduce(x_tensor)
        if op == 'avg':
            x_tensor.div_(get_world_size())
        return x_tensor.item()
    elif isinstance(x, torch.Tensor):
        dist.all_reduce(x)
        if op == 'avg':
            x.div_(get_world_size())
        return x
    else:
        raise TypeError("not supported type: {}".format(type(x)))


synchronize = torch.cuda.synchronize


def get_group(group_size: int) -> List:
    """
    Overview:
        Get the group segmentation of ``group_size`` each group
    Arguments:
        - group_size (:obj:`int`) the ``group_size``
    """
    rank = get_rank()
    world_size = get_world_size()
    if group_size is None:
        group_size = world_size
    assert (world_size % group_size == 0)
    return simple_group_split(world_size, rank, world_size // group_size)


def dist_mode(func: Callable) -> Callable:
    """
    Overview:
        Wrap the function so that in can init and finalize automatically before each call
    Arguments:
        - func (:obj:`Callable`): the function to be wrapped
    """

    def wrapper(*args, **kwargs):
        dist_init()
        func(*args, **kwargs)
        dist_finalize()

    return wrapper


def dist_init(
        backend: str = 'nccl',
        addr: str = None,
        port: str = None,
        rank: int = None,
        world_size: int = None,
        timeout: datetime.timedelta = datetime.timedelta(seconds=60000)
) -> Tuple[int, int]:
    """
    Overview:
        Initialize the distributed training setting.
    Arguments:
        - backend (:obj:`str`): The backend of the distributed training, supports ``['nccl', 'gloo']``.
        - addr (:obj:`str`): The address of the master node.
        - port (:obj:`str`): The port of the master node.
        - rank (:obj:`int`): The rank of the current process.
        - world_size (:obj:`int`): The total number of processes.
        - timeout (:obj:`datetime.timedelta`): The timeout for operations executed against the process group. \
            Default is 60000 seconds.
    """

    assert backend in ['nccl', 'gloo'], backend
    os.environ['MASTER_ADDR'] = addr or os.environ.get('MASTER_ADDR', "localhost")
    os.environ['MASTER_PORT'] = port or os.environ.get('MASTER_PORT', "10314")  # hard-code

    if rank is None:
        local_id = os.environ.get('SLURM_LOCALID', os.environ.get('RANK', None))
        if local_id is None:
            raise RuntimeError("please indicate rank explicitly in dist_init method")
        else:
            rank = int(local_id)
    if world_size is None:
        ntasks = os.environ.get('SLURM_NTASKS', os.environ.get('WORLD_SIZE', None))
        if ntasks is None:
            raise RuntimeError("please indicate world_size explicitly in dist_init method")
        else:
            world_size = int(ntasks)

    dist.init_process_group(backend=backend, rank=rank, world_size=world_size, timeout=timeout)

    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(rank % num_gpus)
    world_size = get_world_size()
    rank = get_rank()
    return rank, world_size


def dist_finalize() -> None:
    """
    Overview:
        Finalize distributed training resources
    """
    # This operation usually hangs out so we ignore it temporally.
    # dist.destroy_process_group()
    pass


class DDPContext:
    """
    Overview:
        A context manager for ``linklink`` distribution
    Interfaces:
        ``__init__``, ``__enter__``, ``__exit__``
    """

    def __init__(self) -> None:
        """
        Overview:
            Initialize the ``DDPContext``
        """

        pass

    def __enter__(self) -> None:
        """
        Overview:
            Initialize ``linklink`` distribution
        """

        dist_init()

    def __exit__(self, *args, **kwargs) -> Any:
        """
        Overview:
            Finalize ``linklink`` distribution
        """

        dist_finalize()


def simple_group_split(world_size: int, rank: int, num_groups: int) -> List:
    """
    Overview:
        Split the group according to ``worldsize``, ``rank`` and ``num_groups``
    Arguments:
        - world_size (:obj:`int`): The world size
        - rank (:obj:`int`): The rank
        - num_groups (:obj:`int`): The number of groups

    .. note::
        With faulty input, raise ``array split does not result in an equal division``
    """
    groups = []
    rank_list = np.split(np.arange(world_size), num_groups)
    rank_list = [list(map(int, x)) for x in rank_list]
    for i in range(num_groups):
        groups.append(dist.new_group(rank_list[i]))
    group_size = world_size // num_groups
    return groups[rank // group_size]


def to_ddp_config(cfg: EasyDict) -> EasyDict:
    """
    Overview:
        Convert the config to ddp config
    Arguments:
        - cfg (:obj:`EasyDict`): The config to be converted
    """

    w = get_world_size()
    if 'batch_size' in cfg.policy:
        cfg.policy.batch_size = int(np.ceil(cfg.policy.batch_size / w))
    if 'batch_size' in cfg.policy.learn:
        cfg.policy.learn.batch_size = int(np.ceil(cfg.policy.learn.batch_size / w))
    if 'n_sample' in cfg.policy.collect:
        cfg.policy.collect.n_sample = int(np.ceil(cfg.policy.collect.n_sample / w))
    if 'n_episode' in cfg.policy.collect:
        cfg.policy.collect.n_episode = int(np.ceil(cfg.policy.collect.n_episode / w))
    return cfg
