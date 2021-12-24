#!/usr/bin/env python3

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# Recommend to initialize NUMA status at the most program begining (before any other imports)
from tutel import system_init
system_init.init_affinity_at_program_beginning()

import os
import time
import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.distributed as dist
from torch import nn
import argparse

import logging

from tutel import moe as tutel_moe

logging.basicConfig(level=logging.INFO)

parser = argparse.ArgumentParser()

parser.add_argument('--local_rank', type=int, default=-1)
parser.add_argument('--batch_size', type=int, default=16)
parser.add_argument('--num_tokens', type=int, default=1024)
parser.add_argument('--model_dim', type=int, default=2048)
parser.add_argument('--hidden_size', type=int, default=2048)
parser.add_argument('--num_local_experts', type=int, default=2)
parser.add_argument('--dtype', type=str, default='float32')
parser.add_argument('--fp32_gate', default=False, action='store_true')
parser.add_argument('--top', type=int, default=2)
parser.add_argument('--l_aux_wt', type=float, default=0.0)
parser.add_argument('--group_count', type=int, default=1)
args = parser.parse_args()

parallel_env = system_init.init_data_model_parallel()
dist_rank, dist_world_size, dist_print = parallel_env.global_rank, parallel_env.global_size, parallel_env.dist_print
args.local_rank = parallel_env.local_device.index

if not parallel_env.is_distributed:
  raise RuntimeError("\nThe current session is not launched in distributed mode. Please run the program with: python3 -m torch.distributed.launch ..")
print(f'Device-{parallel_env.global_rank}: data_rank = {parallel_env.data_rank}, model_rank = {parallel_env.model_rank}')

batch_size = args.batch_size
num_tokens = args.num_tokens
model_dim = args.model_dim
hidden_size = args.hidden_size
num_local_experts = args.num_local_experts
top_value = args.top
device = torch.device('cuda', args.local_rank)

if args.dtype == 'float32':
    torch.set_default_dtype(torch.float32)
elif args.dtype == 'float16':
    torch.set_default_dtype(torch.float16)
elif args.dtype == 'bfloat16':
    torch.set_default_dtype(torch.bfloat16)
else:
    raise Exception('Unrecognized data type specified: %s' % args.dtype)


class ExampleModel(torch.nn.Module):
    def __init__(self):
        super().__init__()

        self._moe_layer = tutel_moe.moe_layer(
            gate_type = {'type': 'top', 'k': top_value, 'fp32_gate': args.fp32_gate},
            experts = {'type': 'ffn', 'count_per_node': num_local_experts, 'hidden_size_per_expert': hidden_size, 'activation_fn': lambda x: F.relu(x)},
            model_dim = model_dim,
            scan_expert_func = lambda name, param: setattr(param, 'skip_allreduce', True),
            seeds = (1, parallel_env.model_rank + 1, 1),
            group = parallel_env.model_group,
        ).to(device)

        # Summary of different parameter types: gate, local_experts
        local_count = sum([torch.numel(param) for name, param in self._moe_layer.get_parameter_iterator(param_type='local_experts')])
        shared_count = sum([torch.numel(param) for name, param in self._moe_layer.get_parameter_iterator(param_type='gate')])
        dist_print('[Statistics] param count for MoE local_experts = %s, param count for MoE gate = %s.\n' % (local_count, shared_count))

    def forward(self, input):
        result = self._moe_layer(input)
        result = F.log_softmax(torch.sum(result, dim=2), dim=1)
        return result

model = ExampleModel()
dist_print(model)

optimizer = torch.optim.SGD(model.parameters(), lr=1e-5)

torch.manual_seed(parallel_env.global_rank)
x = torch.tensor(torch.randn([batch_size, num_tokens, model_dim], dtype=torch.float32, device='cpu').detach().numpy(), dtype=torch.get_default_dtype(), requires_grad=True, device=device)
y = torch.LongTensor(batch_size).random_(1).to(device)

tuples = (parallel_env.global_size, args.dtype, model_dim, hidden_size, batch_size * num_tokens, num_local_experts, top_value, device, parallel_env.group_count)
dist_print('[Benchmark] world_size = %s, dtype = %s, model_dim = %s, hidden_size = %s, samples = %s, num_local_experts = %s, topK = %s, device = `%s`, group_count = %s' % tuples)

average_time, num_steps = 0, 100

params_for_all_reduce = [p for p in model.parameters() if not hasattr(p, 'skip_allreduce') and getattr(p, 'requires_grad', False) and p.grad is not None]
params_for_replicas_all_reduce = [p for p in model.parameters() if not (not hasattr(p, 'skip_allreduce') and getattr(p, 'requires_grad', False) and p.grad is not None)]

for i in range(num_steps):

    torch.cuda.synchronize()
    t_start = time.time()
    optimizer.zero_grad()

    output = model(x)
    loss = F.nll_loss(output, y)
    if args.l_aux_wt:
        loss += args.l_aux_wt * model._moe_layer.l_aux
    loss.backward()
    if parallel_env.global_size > 1:
        for p in params_for_all_reduce:
            p.grad /= parallel_env.global_size
            dist.all_reduce(p.grad, group=parallel_env.global_group)
    if parallel_env.group_count > 1:
        for p in params_for_replicas_all_reduce:
            p.grad /= parallel_env.group_count
            dist.all_reduce(p.grad, group=parallel_env.data_group)
    optimizer.step()

    torch.cuda.synchronize()
    t_stop = time.time()
    dist_print('STEP-%s: DONE, loss = %s, step_time = %s sec.' % (i, float(loss.data), t_stop - t_start))

    if i + 10 >= num_steps:
        average_time += t_stop - t_start

average_time /= 10
dist_print('\n[Summary] Average synchronized step_time = %s sec.' % average_time)
