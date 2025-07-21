# coding=utf-8
# Copyright 2022 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Finetuning any 🤗 Transformers model for image classification leveraging 🤗 Accelerate."""

# 

import argparse
import json
import logging
import math
import os
from pathlib import Path

import datasets
import evaluate
import torch

# from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed

from datasets import load_dataset
from huggingface_hub import HfApi
from torch.utils.data import DataLoader
from torchvision.transforms import (
    CenterCrop,
    Compose,
    Lambda,
    Normalize,
    RandomHorizontalFlip,
    RandomResizedCrop,
    Resize,
    ToTensor,
)
from tqdm.auto import tqdm

import transformers
from transformers import AutoConfig, AutoImageProcessor, AutoModelForImageClassification, SchedulerType, get_scheduler
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version

import torch.backends.cudnn as cudnn
import torch.multiprocessing as mp
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data.distributed



from base_lib.helper import get_communicator
# import horovod.torch as hvd
# import base_lib.hv_distributed_optimizer_buffer as hvd
# import base_lib.optimizer_0621 as hvd


import hv_distributed_optimizer as hvd
from compression import compressors
# from utils_model import get_network




# import horovod.torch as hvd
# import base_lib.optimizer_adtopk_hvd as hvd
import os
import math
from tqdm import tqdm
from base_lib.helper import get_communicator

# from utils_model import get_network



os.environ['HOROVOD_FUSION_THRESHOLD'] = '0'
os.environ['HOROVOD_CACHE_CAPACITY'] = '0'
# os.environ['HOROVOD_CYCLE_TIME'] = '0'


import numpy as np
import matplotlib.pyplot as plt
import time
import timeit
import numpy as np
from base_lib.profiling import benchmark

import uuid
import shutil
from typing import Dict, Optional
import torchsnapshot
from torchsnapshot import Snapshot, Stateful


import numpy as np

import numpy as np
import matplotlib.pyplot as plt
import time
import timeit
import numpy as np
from base_lib.profiling import benchmark


# try:
#     mp.set_start_method('spawn')
# except RuntimeError:
#     pass



# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
# check_min_version("4.43.0.dev0")

# logger = get_logger(__name__)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.WARNING)


logger = logging.getLogger(__name__)

require_version("datasets>=2.0.0", "To fix: pip install -r examples/pytorch/image-classification/requirements.txt")


def parse_args():
    parser = argparse.ArgumentParser(description="Fine-tune a Transformers model on an image classification dataset")
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        # default="cifar100",
        # default="beans",
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset)."
        ),
    )
    parser.add_argument("--train_dir", type=str, 
                        # default='/data/cifar100', 
                        default='/data/beans/train',
                        # default=None, 
                        help="A folder containing the training data.")
    parser.add_argument("--validation_dir", type=str, 
                        # default='/data/cifar100', 
                        default='/data/beans/validation',
                        # default=None, 
                        help="A folder containing the validation data.")
    
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--max_eval_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--train_val_split",
        type=float,
        default=0.15,
        help="Percent to split off of train for validation",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        # default="google/vit-base-patch16-224-in21k",
        default="/data/google/vit-base-patch16-224-in21k",
    )
    parser.add_argument(
        "--metric_accuracy",
        type=str,
        help="Metric accuracy.",
        # default="google/vit-base-patch16-224-in21k",
        default="/data/google/evaluate/metrics/accuracy",
    )
    
    
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    
    parser.add_argument("--num_train_epochs", type=int, 
                        default=3, 
                        help="Total number of training epochs to perform.")
    
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    
    
    # parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--trust_remote_code",
        action="store_true",
        help=(
            "Whether to trust the execution of code from datasets/models defined on the Hub."
            " This option should only be set to `True` for repositories you trust and in which you have read the"
            " code, as it will execute code present on the Hub on your local machine."
        ),
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations. '
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--ignore_mismatched_sizes",
        action="store_true",
        help="Whether or not to enable to load a pretrained model whose head dimensions are different.",
    )
    parser.add_argument(
        "--image_column_name",
        type=str,
        default="image",
        help="The name of the dataset column containing the image data. Defaults to 'image'.",
    )
    parser.add_argument(
        "--label_column_name",
        type=str,
        default="label",
        help="The name of the dataset column containing the labels. Defaults to 'label'.",
    )
    
    parser.add_argument(
        '--gpu', 
        action='store_true', 
        default=True, help='use gpu or not'
    )
    parser.add_argument(
        '--no-cuda', 
        action='store_true', 
        default=False, 
        help='disables CUDA training'
    )
    parser.add_argument("--seed", type=int, default=42, help="A seed for reproducible training.")
    
    
    # parser.add_argument('--seed', type=int, default=42, help='random seed')
    parser.add_argument('--fp16',
                        default=False,
                        # action='store_true',
                        help="Mixed precision training")
    # 
    parser.add_argument('--use-adasum', action='store_true', default=False,
                    help='use adasum algorithm to do reduction')
    
    # Elastic Horovod settings
    parser.add_argument('--batches-per-commit', type=int, default=50,
                    help='number of batches processed before calling `state.commit()`; '
                         'commits prevent losing progress if an error occurs, but slow '
                         'down training.')
    parser.add_argument('--batches-per-host-check', type=int, default=5,
                    help='number of batches processed before calling `state.check_host_updates()`; '
                         'this check is very fast compared to state.commit() (which calls this '
                         'as part of the commit process), but because still incurs some cost due '
                         'to broadcast, so we may not want to perform it every batch.')

    
    # Gradient Merging    
    parser.add_argument('--model-net', default='gpt_2', type=str, help='net type')
    
    parser.add_argument('--model', type=str, default='gpt_2',
                    help='model to benchmark')
    
    parser.add_argument('--num-warmup-batches', type=int, default=20,
                        help='number of warm-up batches that don\'t count towards benchmark')
    parser.add_argument('--num-batches-per-iter', type=int, default=10,
                        help='number of batches per benchmark iteration')
    parser.add_argument('--num-iters', type=int, default=50,
                        help='number of benchmark iterations')
    
    
    parser.add_argument('--mgwfbp', action='store_true', default=False, help='Use MG-WFBP')
    parser.add_argument('--asc', action='store_true', default=False, help='Use MG-WFBP')
    parser.add_argument('--nstreams', type=int, default=1, help='Number of communication streams')

    
    parser.add_argument('--threshold', type=int, default=34015396, help='Set threshold if mgwfbp is False')
    parser.add_argument('--rdma', action='store_true', default=False, help='Use RDMA')

    
    parser.add_argument('--compressor', type=str, default='dgc', help='Specify the compressors if density < 1.0')
    
    parser.add_argument('--memory', type=str, default = 'residual', help='Error-feedback')
    parser.add_argument('--density', type=float, default=0.01, help='Density for sparsification')
    
    parser.add_argument('--percent', type=float, default=0, help='percent of residual 0')    args = parser.parse_args()

    # Sanity checks
    if args.dataset_name is None and args.train_dir is None and args.validation_dir is None:
        raise ValueError("Need either a dataset name or a training/validation folder.")

    if args.push_to_hub or args.with_tracking:
        if args.output_dir is None:
            raise ValueError(
                "Need an `output_dir` to create a repo when `--push_to_hub` or `with_tracking` is specified."
            )

    if args.output_dir is not None:
        os.makedirs(args.output_dir, exist_ok=True)

    return args


def main():
    args = parse_args()

    args.cuda = not args.no_cuda and torch.cuda.is_available()
    
    # allreduce_batch_size = args.batch_size * args.batches_per_allreduce
    hvd.init()
    torch.manual_seed(args.seed)

    if args.cuda:
        # Horovod: pin GPU to local rank.
        torch.cuda.set_device(hvd.local_rank())
        torch.cuda.manual_seed(args.seed)

    # cudnn.benchmark = True
    # Horovod: limit # of CPU threads to be used per worker.
    torch.set_num_threads(4)


    kwargs = {'num_workers': 4, 'pin_memory': True} if args.cuda else {}
    # When supported, use 'forkserver' to spawn dataloader workers instead of 'fork' to prevent
    # issues with Infiniband implementations that are not fork-safe
    if (kwargs.get('num_workers', 0) > 0 and hasattr(mp, '_supports_context') and
            mp._supports_context and 'forkserver' in mp.get_all_start_methods()):
        kwargs['multiprocessing_context'] = 'forkserver'


    # # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # # information sent is the one passed as arguments along with your Python/PyTorch versions.
    # send_example_telemetry("run_clm_no_trainer", args)

    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    send_example_telemetry("run_image_classification_no_trainer", args)

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment
    accelerator_log_kwargs = {}

    if args.with_tracking:
        accelerator_log_kwargs["log_with"] = args.report_to
        accelerator_log_kwargs["project_dir"] = args.output_dir

    # accelerator = Accelerator(gradient_accumulation_steps=args.gradient_accumulation_steps, **accelerator_log_kwargs)

    # logger.info(accelerator.state)
    
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    # logger.info(accelerator.state, main_process_only=False)
    
    # if accelerator.is_local_main_process:
    #     datasets.utils.logging.set_verbosity_warning()
    #     transformers.utils.logging.set_verbosity_info()
    # else:
    #     datasets.utils.logging.set_verbosity_error()
    #     transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)


    # Handle the repository creation
    # if accelerator.is_main_process:
    #     if args.push_to_hub:
    #         # Retrieve of infer repo_name
    #         repo_name = args.hub_model_id
    #         if repo_name is None:
    #             repo_name = Path(args.output_dir).absolute().name
    #         # Create repo and retrieve repo_id
    #         api = HfApi()
    #         repo_id = api.create_repo(repo_name, exist_ok=True, token=args.hub_token).repo_id

    #         with open(os.path.join(args.output_dir, ".gitignore"), "w+") as gitignore:
    #             if "step_*" not in gitignore:
    #                 gitignore.write("step_*\n")
    #             if "epoch_*" not in gitignore:
    #                 gitignore.write("epoch_*\n")
    #     elif args.output_dir is not None:
    #         os.makedirs(args.output_dir, exist_ok=True)
    # accelerator.wait_for_everyone()
    

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    print('download the dataset')
    time_load_dataset = time.time()
    if args.dataset_name is not None:
        
        # Downloading and loading a dataset from the hub.
        # dataset = load_dataset(path='/data/cifar100/train', name= args.dataset_name, trust_remote_code=args.trust_remote_code)
        dataset = load_dataset(args.dataset_name, trust_remote_code=args.trust_remote_code)
        
    else:
        print('load the dataset')
        data_files = {}
        if args.train_dir is not None:
            data_files["train"] = os.path.join(args.train_dir, "**")
        if args.validation_dir is not None:
            # data_files["validation"] = os.path.join(args.validation_dir, "**")
            data_files["validation"] = os.path.join(args.validation_dir, "**")
        
        print(data_files)
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
            # split='train',
        )
        
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.0.0/en/image_process#imagefolder.
    
    if hvd.rank() == 0:
        print('time_load_dataset = ', time.time()-time_load_dataset)
    
    
    dataset_column_names = dataset["train"].column_names if "train" in dataset else dataset["validation"].column_names
    
    print('dataset_column_names = ', dataset_column_names)
    print('----------------------------------------------')
    
    
    if args.image_column_name not in dataset_column_names:
        raise ValueError(
            f"--image_column_name {args.image_column_name} not found in dataset '{args.dataset_name}'. "
            "Make sure to set `--image_column_name` to the correct audio column - one of "
            f"{', '.join(dataset_column_names)}."
        )
    if args.label_column_name not in dataset_column_names:
        raise ValueError(
            f"--label_column_name {args.label_column_name} not found in dataset '{args.dataset_name}'. "
            "Make sure to set `--label_column_name` to the correct text column - one of "
            f"{', '.join(dataset_column_names)}."
        )

    # If we don't have a validation split, split off a percentage of train as validation.
    args.train_val_split = None if "validation" in dataset.keys() else args.train_val_split
    if isinstance(args.train_val_split, float) and args.train_val_split > 0.0:
        split = dataset["train"].train_test_split(args.train_val_split)
        dataset["train"] = split["train"]
        dataset["validation"] = split["test"]

    # Prepare label mappings.
    # We'll include these in the model's config to get human readable labels in the Inference API.
    labels = dataset["train"].features[args.label_column_name].names
    label2id = {label: str(i) for i, label in enumerate(labels)}
    id2label = {str(i): label for i, label in enumerate(labels)}
    
    print('Load pretrained model and image processor')
    # Load pretrained model and image processor
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        args.model_name_or_path,
        num_labels=len(labels),
        i2label=id2label,
        label2id=label2id,
        finetuning_task="image-classification",
        trust_remote_code=args.trust_remote_code,
    )
    
    print('AutoImageProcessor.from_pretrained')
    image_processor = AutoImageProcessor.from_pretrained(
        args.model_name_or_path,
        trust_remote_code=args.trust_remote_code,
    )
    
    print('AutoModelForImageClassification.from_pretrained')
    model = AutoModelForImageClassification.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
        ignore_mismatched_sizes=args.ignore_mismatched_sizes,
        trust_remote_code=args.trust_remote_code,
    )
    
    print('Preprocessing the datasets')
    
    # Preprocessing the datasets

    # Define torchvision transforms to be applied to each image.
    if "shortest_edge" in image_processor.size:
        size = image_processor.size["shortest_edge"]
    else:
        size = (image_processor.size["height"], image_processor.size["width"])
    normalize = (
        Normalize(mean=image_processor.image_mean, std=image_processor.image_std)
        if hasattr(image_processor, "image_mean") and hasattr(image_processor, "image_std")
        else Lambda(lambda x: x)
    )
    train_transforms = Compose(
        [
            RandomResizedCrop(size),
            RandomHorizontalFlip(),
            ToTensor(),
            normalize,
        ]
    )
    val_transforms = Compose(
        [
            Resize(size),
            CenterCrop(size),
            ToTensor(),
            normalize,
        ]
    )
    print('val_transforms')

    def preprocess_train(example_batch):        
        """Apply _train_transforms across a batch."""
        example_batch["pixel_values"] = [
            train_transforms(image.convert("RGB")) for image in example_batch[args.image_column_name]
        ]
        return example_batch

    def preprocess_val(example_batch):
        """Apply _val_transforms across a batch."""
        example_batch["pixel_values"] = [
            val_transforms(image.convert("RGB")) for image in example_batch[args.image_column_name]
        ]
        return example_batch
    
        
    # with accelerator.main_process_first():
    if True:
        if args.max_train_samples is not None:
            dataset["train"] = dataset["train"].shuffle(seed=args.seed).select(range(args.max_train_samples))
        # Set the training transforms
        train_dataset = dataset["train"].with_transform(preprocess_train)
        if args.max_eval_samples is not None:
            dataset["validation"] = dataset["validation"].shuffle(seed=args.seed).select(range(args.max_eval_samples))
        # Set the validation transforms
        eval_dataset = dataset["validation"].with_transform(preprocess_val)
    
    

    # DataLoaders creation:
    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        labels = torch.tensor([example[args.label_column_name] for example in examples])
        return {"pixel_values": pixel_values, "labels": labels}
    
    
    print('train_dataloader')
    
    # train_dataloader = DataLoader(
    #     train_dataset, shuffle=True, collate_fn=collate_fn, batch_size=args.per_device_train_batch_size
    # )
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_dataset, num_replicas=hvd.size(), rank=hvd.rank())    
    train_dataloader = DataLoader(
        train_dataset,  collate_fn = collate_fn, batch_size=args.per_device_train_batch_size,
        sampler=train_sampler, **kwargs)
    
    # eval_dataloader = DataLoader(eval_dataset, collate_fn=collate_fn, batch_size=args.per_device_eval_batch_size)
    val_sampler = torch.utils.data.distributed.DistributedSampler(
        eval_dataset, num_replicas=hvd.size(), rank=hvd.rank())
    eval_dataloader = DataLoader(
        eval_dataset, collate_fn=collate_fn, batch_size=args.per_device_eval_batch_size,
        sampler=val_sampler, **kwargs)
    

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True
    
    if hvd.rank() == 0:
        print("---args.num_train_epochs: ", args.num_train_epochs)
        print("---args.max_train_steps: ", args.max_train_steps)
        print("---len(train_dataloader): ", len(train_dataloader))

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps * hvd.size(),
        
        num_training_steps=args.max_train_steps
        if overrode_max_train_steps
        else args.max_train_steps * hvd.size(),
    )
    
    print('Prepare accelerator')
    
    
    ### model to cuda
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    model.to(device)
    
    from base_lib.helper import get_communicator
    if args.density < 1:
        communicator_str = 'allgather'
    else:
        communicator_str = 'allreduce'
        args.compressor ='none'
        args.memory = 'none'
        
    params = {'compressor': args.compressor, 
              'memory': args.memory, 
              'density': args.density,
              'communicator': communicator_str,
              'model_named_parameters':model.named_parameters()
            }
      
    seq_layernames, layerwise_times = None, None    optimizer = hvd.DistributedOptimizer(args.model_net,
                                         optimizer, 
                                        #  model= model,
                                         named_parameters=model.named_parameters(), 
                                         compression=compressors[args.compressor](), 
                                         is_sparse=args.density<1, 
                                         density=args.density, 
                                         seq_layernames=seq_layernames, 
                                         layerwise_times=layerwise_times, 
                                         norm_clip=None, 
                                         threshold=args.threshold, 
                                         writer=None, 
                                         gradient_path='./', 
                                         momentum_correction=False, 
                                         fp16=args.fp16, 
                                         mgwfbp=args.mgwfbp, 
                                         rdma=args.rdma, 
                                         asc=args.asc
                                    )    
    
    state_time = time.time()
    
    resume_from_epoch = 0


    print('state_time = ', time.time() - state_time)

    if hvd.rank()==0:
        print('state_time = ', time.time() - state_time)
    
    _state_dict_cpu = {}
    numel_count = 0
    layers_count = 0
    for key, value in model.state_dict().items():
        t_cpu = torch.zeros(value.numel(), device='cpu', dtype=value.dtype, requires_grad=False)
        _state_dict_cpu[key] = t_cpu
        numel_count += value.numel()
        layers_count +=1
    
    _state_dict_gpu_flatten = torch.zeros(numel_count, device=value.device, dtype=value.dtype, requires_grad=False)
    
    if hvd.rank() == 0:
        # print(model.state_dict().items())
        print('model.state_dict().keys() = ', model.state_dict().keys())
        
        print('layers_count = ', layers_count)
        
        print('numel_count = ', numel_count)
        
        print('optimizer.state_dict() = ', optimizer.state_dict().keys())
        
        pass

    # torchsnapshot
    progress = torchsnapshot.StateDict(current_epoch=0)
    # torchsnapshot: define app state
    app_state: Dict[str, Stateful] = {
        "rng_state": torchsnapshot.RNGState(),
        "model": model,
        "optim": optimizer,
        "progress": progress,
    }
    
    snapshot: Optional[Snapshot] = None
    checkpoint_save_work_dir = './gpt2_checkpoint/gpt2_base'

    optimizer.app_state = app_state
    optimizer.checkpoint_save_work_dir = checkpoint_save_work_dir
    

    # Train, Train, Train
    

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)
    
    print('Figure out how many')
    # Figure out how many steps we should save the Accelerator states
    checkpointing_steps = args.checkpointing_steps
    if checkpointing_steps is not None and checkpointing_steps.isdigit():
        checkpointing_steps = int(checkpointing_steps)


    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value
        
        # accelerator.init_trackers("image_classification_no_trainer", experiment_config)
    
    print('Get the metric function')
    
    # Get the metric function
    # metric = evaluate.load("accuracy")
    # metric = evaluate.load("/data/google/evaluate/metrics/accuracy")
    metric = evaluate.load(args.metric_accuracy)
    
    
    print('Train!')
    # Train!
    # total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps
    total_batch_size = args.per_device_train_batch_size * hvd.size() * args.gradient_accumulation_steps



    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    
    # progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    # progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            checkpoint_path = args.resume_from_checkpoint
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
            checkpoint_path = path
            path = os.path.basename(checkpoint_path)

        # accelerator.print(f"Resumed from checkpoint: {checkpoint_path}")
        # accelerator.load_state(checkpoint_path)
        
        
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
            completed_steps = starting_epoch * num_update_steps_per_epoch
        else:
            # need to multiply `gradient_accumulation_steps` to reflect real steps
            resume_step = int(training_difference.replace("step_", "")) * args.gradient_accumulation_steps
            starting_epoch = resume_step // len(train_dataloader)
            completed_steps = resume_step // args.gradient_accumulation_steps
            resume_step -= starting_epoch * len(train_dataloader)

    # update the progress_bar if load from checkpoint
    # progress_bar.update(completed_steps)
    verbose = 1 if hvd.rank() == 0 else 0
    
    print('args.with_tracking = ', args.with_tracking)
    for epoch in range(starting_epoch, args.num_train_epochs):
        model.train()
        if args.with_tracking:
            total_loss = 0
        # if args.resume_from_checkpoint and epoch == starting_epoch and resume_step is not None:
        #     # We skip the first `n` batches in the dataloader when resuming from a checkpoint
        #     active_dataloader = accelerator.skip_first_batches(train_dataloader, resume_step)
        # else:
        #     active_dataloader = train_dataloader
        
        active_dataloader = train_dataloader
        with tqdm(total=len(active_dataloader),
              desc='Train Epoch     #{}'.format(epoch + 1),
              disable=not verbose) as t:
                    
            for step, batch in enumerate(active_dataloader):
                # with accelerator.accumulate(model):
                # if True:
                with torch.no_grad():
                    # optimizer.zero_grad()
                    ### to cuda
                    batch = {key: value.to(device) for key, value in batch.items()}
                    
                    outputs = model(**batch)
                    loss = outputs.loss
                    # We keep track of the loss at each epoch
                    if args.with_tracking:
                        total_loss += loss.detach().float()
                    # accelerator.backward(loss)
                    
                    loss.requires_grad = True
                    loss.backward()
                    
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()
                
                # print('optimizer.zero_grad()')
                t.update(1)
            
                
                
    
                if isinstance(checkpointing_steps, int):
                    if completed_steps % checkpointing_steps == 0:
                        output_dir = f"step_{completed_steps}"
                        if args.output_dir is not None:
                            output_dir = os.path.join(args.output_dir, output_dir)
                    
                        # accelerator.save_state(output_dir)

                        # if args.push_to_hub and epoch < args.num_train_epochs - 1:
                        #     accelerator.wait_for_everyone()
                        #     unwrapped_model = accelerator.unwrap_model(model)
                        #     unwrapped_model.save_pretrained(
                        #         args.output_dir,
                        #         is_main_process=accelerator.is_main_process,
                        #         save_function=accelerator.save,
                        #     )
                        #     if accelerator.is_main_process:
                        #         image_processor.save_pretrained(args.output_dir)
                        #         api.upload_folder(
                        #             commit_message=f"Training in progress epoch {epoch}",
                        #             folder_path=args.output_dir,
                        #             repo_id=repo_id,
                        #             repo_type="model",
                        #             token=args.hub_token,
                        #         )

                if completed_steps >= args.max_train_steps:
                    break
        
        
        model.eval()
        losses = []
        for step, batch in enumerate(eval_dataloader):
            with torch.no_grad():
                ### to cuda
                batch = {key: value.to(device) for key, value in batch.items()}
            
                
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            
            # predictions, references = accelerator.gather_for_metrics((predictions, batch["labels"]))
            references = outputs.loss
            losses.append(references)
            
            # metric.add_batch(
            #     predictions=predictions,
            #     # references=references,
            # )

        # eval_metric = metric.compute()
        
        
        losses = [loss.unsqueeze(0) for loss in losses]
        losses = torch.cat(losses)
        try:
            eval_loss = torch.mean(losses)
            perplexity = math.exp(eval_loss)
        except OverflowError:
            perplexity = float("inf")
            
        logger.info(f"epoch {epoch}: {eval_loss}")

        # if args.with_tracking:
        #     accelerator.log(
        #         {
        #             "accuracy": eval_metric,
        #             "train_loss": total_loss.item() / len(train_dataloader),
        #             "epoch": epoch,
        #             "step": completed_steps,
        #         },
        #         step=completed_steps,
        #     )

        # if args.push_to_hub and epoch < args.num_train_epochs - 1:
        #     accelerator.wait_for_everyone()
        #     unwrapped_model = accelerator.unwrap_model(model)
        #     unwrapped_model.save_pretrained(
        #         args.output_dir, is_main_process=accelerator.is_main_process, save_function=accelerator.save
        #     )
        #     if accelerator.is_main_process:
        #         image_processor.save_pretrained(args.output_dir)
        #         api.upload_folder(
        #             commit_message=f"Training in progress epoch {epoch}",
        #             folder_path=args.output_dir,
        #             repo_id=repo_id,
        #             repo_type="model",
        #             token=args.hub_token,
        #         )

        # if args.checkpointing_steps == "epoch":
        #     output_dir = f"epoch_{epoch}"
        #     if args.output_dir is not None:
        #         output_dir = os.path.join(args.output_dir, output_dir)
        #     accelerator.save_state(output_dir)

    

    # if args.output_dir is not None:
    #     accelerator.wait_for_everyone()
    #     unwrapped_model = accelerator.unwrap_model(model)
    #     unwrapped_model.save_pretrained(
    #         args.output_dir, is_main_process=accelerator.is_main_process, save_function=accelerator.save
    #     )
    #     if accelerator.is_main_process:
    #         image_processor.save_pretrained(args.output_dir)
    #         if args.push_to_hub:
    #             api.upload_folder(
    #                 commit_message="End of training",
    #                 folder_path=args.output_dir,
    #                 repo_id=repo_id,
    #                 repo_type="model",
    #                 token=args.hub_token,
    #             )
    #         all_results = {f"eval_{k}": v for k, v in eval_metric.items()}
    #         with open(os.path.join(args.output_dir, "all_results.json"), "w") as f:
    #             json.dump(all_results, f)


if __name__ == "__main__":
    main()
