import multiprocessing
import pickle
import subprocess
from argparse import ArgumentParser
from enum import Flag
from multiprocessing import Process

import horovod.tensorflow.keras as hvd
import tensorflow as tf
import zmq
from pyarrow import deserialize
from tensorflow.keras.backend import set_session

from common import (create_experiment_dir, init_components, load_yaml_config,
                    save_yaml_config)
from core.mem_pool import MemPoolManager, MultiprocessingMemPool
from utils import logger
from utils.cmdline import parse_cmdline_kwargs

# Horovod: initialize Horovod.
hvd.init()

# Horovod: pin GPU to be used to process local rank (one GPU per process)
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
config.gpu_options.visible_device_list = str(hvd.local_rank())
set_session(tf.Session(config=config))

parser = ArgumentParser()
parser.add_argument('--alg', type=str, default='ppo', help='The RL algorithm')
parser.add_argument('--env', type=str, default='PongNoFrameskip-v4', help='The game environment')
parser.add_argument('--num_steps', type=int, default=10000000, help='The number of total training steps')
parser.add_argument('--data_port', type=int, default=5000, help='Learner server port to receive training data')
parser.add_argument('--param_port', type=int, default=5001, help='Learner server to publish model parameters')
parser.add_argument('--model', type=str, default='accnn', help='Training model')
parser.add_argument('--pool_size', type=int, default=1280, help='The max length of data pool')
parser.add_argument('--training_freq', type=int, default=1,
                    help='How many receptions of new data are between each training, '
                         'which can be fractional to represent more than one training per reception')
parser.add_argument('--keep_training', type=bool, default=False,
                    help="No matter whether new data is received recently, keep training as long as the data is enough "
                         "and ignore `--training_freq`")
parser.add_argument('--batch_size', type=int, default=1280, help='The batch size for training')
parser.add_argument('--exp_path', type=str, default=None, help='Directory to save logging data and config file')
parser.add_argument('--config', type=str, default=None, help='The YAML configuration file')
parser.add_argument('--record_throughput_interval', type=int, default=10,
                    help='The time interval between each throughput record')
parser.add_argument('--num_envs', type=int, default=1, help='The number of environment copies')
parser.add_argument('--ckpt_save_freq', type=int, default=10, help='The number of updates between each weights saving')
parser.add_argument('--ckpt_save_type', type=str, default='weight', help='Type of checkpoint file will be recorded : weight(smaller) or checkpoint(bigger')


def main():
    # Parse input parameters
    args, unknown_args = parser.parse_known_args()
    args.num_steps = int(args.num_steps)
    unknown_args = parse_cmdline_kwargs(unknown_args)

    # Load config file
    load_yaml_config(args, 'learner')

    # Expose socket to actor(s)
    context = zmq.Context()
    weights_socket = context.socket(zmq.PUB)
    weights_socket.bind(f'tcp://*:{args.param_port}')

    _, agent = init_components(args, unknown_args)

    # Configure experiment directory
    create_experiment_dir(args, 'LEARNER-')    
    save_yaml_config(args.exp_path / 'config.yaml', args, 'learner', agent)
    args.log_path = args.exp_path / 'log'
    args.ckpt_path = args.exp_path / 'ckpt'
    args.ckpt_path.mkdir()
    args.log_path.mkdir()

    logger.configure(str(args.log_path))


    # Record commit hash
    with open(args.exp_path / 'hash', 'w') as f:
        f.write(str(subprocess.run('git rev-parse HEAD'.split(), stdout=subprocess.PIPE).stdout.decode('utf-8')))

    # Variables to control the frequency of training
    receiving_condition = multiprocessing.Condition()
    num_receptions = multiprocessing.Value('i', 0)

    # Start memory pool in another process
    manager = MemPoolManager()
    manager.start()
    mem_pool = manager.MemPool(capacity=args.pool_size)
    Process(target=recv_data,
            args=(args.data_port, mem_pool, receiving_condition, num_receptions, args.keep_training)).start()

    # Print throughput statistics
    Process(target=MultiprocessingMemPool.record_throughput, args=(mem_pool, args.record_throughput_interval)).start()

    freq = 0
    learn_flag = 0
    while True:
        if learn_flag == 0:
            weights_socket.send(pickle.dumps(agent.get_weights()))

        if len(mem_pool) >= args.batch_size:

            # Sync weights to actor
            weights = agent.get_weights()
            if hvd.rank() == 0:
                weights_socket.send(pickle.dumps(weights))
            
            if freq%args.ckpt_save_freq == 0:
                if args.ckpt_save_type == 'checkpoint':
                    agent.save(args.ckpt_path / 'ckpt')
                elif args.ckpt_save_type == 'weight':
                    with open(args.ckpt_path / 'weight.ckpt', 'wb') as f:
                        pickle.dump(weights, f)

            if args.keep_training:
                agent.learn(mem_pool.sample(size=args.batch_size))
            else:
                with receiving_condition:
                    while num_receptions.value < args.training_freq:
                        receiving_condition.wait()
                    data = mem_pool.sample(size=args.batch_size)
                    num_receptions.value -= args.training_freq
                # Training
                stat = agent.learn(data)
                learn_flag = 1
                if stat is not None:
                    for k, v in stat.items():
                        logger.record_tabular(k, v)
                logger.dump_tabular()

            freq += 1


def recv_data(data_port, mem_pool, receiving_condition, num_receptions, keep_training):
    context = zmq.Context()
    data_socket = context.socket(zmq.REP)
    data_socket.bind(f'tcp://*:{data_port}')

    while True:
        # noinspection PyTypeChecker
        data: dict = deserialize(data_socket.recv())
        data_socket.send(b'200')

        if keep_training:
            mem_pool.push(data)
        else:
            with receiving_condition:
                mem_pool.push(data)
                num_receptions.value += 1
                receiving_condition.notify()


if __name__ == '__main__':
    main()
