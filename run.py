#!/bin/sh
''''exec python -u -- "$0" ${1+"$@"} # '''
import logging
import os
import os.path as osp
import sys
from multiprocessing import Process, Queue
import pref_interface

import easy_tf_log
import gym
import gym_moving_dot

from args import parse_args
from enduro_wrapper import EnduroWrapper
from openai_baselines import logger
from openai_baselines.a2c.a2c import learn
from openai_baselines.a2c.policies import MlpPolicy, CnnPolicy
from openai_baselines.common import set_global_seeds
from openai_baselines.common.atari_wrappers import wrap_deepmind_nomax
from openai_baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from pref_interface import PrefInterface, recv_prefs
from reward_predictor import RewardPredictorEnsemble
from utils import vid_proc, get_port_range, load_pref_db, PrefDB

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'  # filter out INFO messages


def configure_a2c_logger(log_dir):
    a2c_dir = osp.join(log_dir, 'a2c')
    os.makedirs(a2c_dir)
    tb = logger.TensorBoardOutputFormat(a2c_dir)
    logger.Logger.CURRENT = logger.Logger(dir=a2c_dir, output_formats=[tb])


def run(general_args, a2c_args, pref_interface_args, reward_predictor_training_args):
    seg_pipe = Queue()
    pref_pipe = Queue()
    start_policy_training_flag = Queue(maxsize=1)

    if general_args['render_episodes']:
        episode_vid_queue = start_episode_renderer()
    else:
        episode_vid_queue = None

    def make_reward_predictor(name, cluster_dict):
        return RewardPredictorEnsemble(
            name=name,
            cluster_dict=cluster_dict,
            log_dir=general_args['log_dir'],
            batchnorm=reward_predictor_training_args['batchnorm'],
            dropout=reward_predictor_training_args['dropout'],
            lr=reward_predictor_training_args['lr'],
            network=reward_predictor_training_args['network'])

    if general_args['mode'] == 'gather_initial_prefs':
        env, a2c_proc = start_policy_training(cluster_dict=None,
                                              make_reward_predictor=None,
                                              go_pipe=start_policy_training_flag, seg_pipe=seg_pipe, episode_vid_queue=episode_vid_queue,
                                              gen_segments=True,
                                              log_dir=general_args['log_dir'],
                                              **a2c_args)
        pi_proc = start_pref_interface(seg_pipe=seg_pipe, pref_pipe=pref_pipe,
                                       **pref_interface_args)
        pref_db_train, pref_db_val = pref_interface.get_initial_prefs(pref_pipe=pref_pipe,
                                                                      n_initial_prefs=general_args['n_initial_prefs'],
                                                                      db_max=general_args['db_max'])
        pref_interface.save_prefs(pref_db_train=pref_db_train, pref_db_val=pref_db_val,
                                  save_dir=general_args['log_dir'], name='initial')
        pi_proc.terminate()
        a2c_proc.terminate()
        env.close()
    elif general_args['mode'] == 'pretrain_reward_predictor':
        cluster_dict = create_cluster_dict(['ps', 'train'])
        ps_proc = start_parameter_server(cluster_dict, make_reward_predictor)
        reward_predictor_training_proc = start_reward_predictor_training(just_pretrain=True,
                                                                         pref_pipe=pref_pipe, go_pipe=start_policy_training_flag,
                                                                         make_reward_predictor=make_reward_predictor,
                                                                         n_initial_epochs=reward_predictor_training_args['n_initial_epochs'],
                                                                         db_max=general_args['db_max'],
                                                                         prefs_dir=general_args['prefs_dir'],
                                                                         val_interval=reward_predictor_training_args['val_interval'],
                                                                         ckpt_interval=reward_predictor_training_args['ckpt_interval'])
        reward_predictor_training_proc.join()
        ps_proc.terminate()
    elif general_args['mode'] == 'train_policy_with_original_rewards':
        env, a2c_proc = start_policy_training(cluster_dict=None, make_reward_predictor=None,
                                         go_pipe=start_policy_training_flag, seg_pipe=seg_pipe, episode_vid_queue=episode_vid_queue,
                                         gen_segments=False,
                                         log_dir=general_args['log_dir'],
                                         **a2c_args)
        start_policy_training_flag.put(True)
        a2c_proc.join()
        env.close()
    elif general_args['mode'] == 'train_policy_with_preferences':
        cluster_dict = create_cluster_dict(['ps', 'train'])
        ps_proc = start_parameter_server(cluster_dict, make_reward_predictor)
        env, a2c_proc = start_policy_training(cluster_dict=cluster_dict, make_reward_predictor=None,
                                              go_pipe=start_policy_training_flag, seg_pipe=seg_pipe, episode_vid_queue=episode_vid_queue,
                                              gen_segments=True,
                                              log_dir=general_args['log_dir'],
                                              **a2c_args)
        pi_proc = start_pref_interface(seg_pipe=seg_pipe, pref_pipe=pref_pipe,
                                       **pref_interface_args)
        reward_predictor_training_proc = start_reward_predictor_training(cluster_dict=cluster_dict,
                                                                         ckpt_path=reward_predictor_training_args['ckpt_path'],
                                                                         just_pretrain=False,
                                                                         pref_pipe=pref_pipe, go_pipe=start_policy_training_flag,
                                                                         make_reward_predictor=make_reward_predictor,
                                                                         n_initial_epochs=reward_predictor_training_args['n_initial_epochs'],
                                                                         db_max=general_args['db_max'],
                                                                         prefs_dir=general_args['prefs_dir'],
                                                                         val_interval=reward_predictor_training_args['val_interval'],
                                                                         ckpt_interval=reward_predictor_training_args['ckpt_interval'])
        a2c_proc.join()
        reward_predictor_training_proc.terminate()
        pi_proc.terminate()
        ps_proc.terminate()
        env.close()
    else:
        raise Exception("Unknown mode: {}".format(general_args['mode']))


def create_cluster_dict(parts_to_run):
    ports = get_port_range(start_port=2200,
                           n_ports=len(parts_to_run) + 1,
                           random_stagger=True)
    cluster_dict = {}
    for part, port in zip(parts_to_run, ports):
        cluster_dict[part] = ['localhost:{}'.format(port)]
    return cluster_dict


def start_parameter_server(cluster_dict, make_reward_predictor):
    def ps():
        reward_predictor = make_reward_predictor('ps', cluster_dict)
        reward_predictor.server.join()
    ps_proc = Process(target=ps, daemon=True)
    ps_proc.start()
    return ps_proc


def start_policy_training(cluster_dict, make_reward_predictor, env_id, n_envs, seed, seg_pipe, go_pipe, gen_segments,
                          log_dir, lr_scheduler, num_timesteps, ckpt_dir, episode_vid_queue, ent_coef, ckpt_interval):
    if env_id == 'MovingDotNoFrameskip-v0':
        policy_fn = MlpPolicy
    elif env_id == 'PongNoFrameskip-v4' or env_id == 'EnduroNoFrameskip-v4':
        policy_fn = CnnPolicy
    else:
        raise Exception("Unsure about policy network architecture for {}".format(env_id))

    configure_a2c_logger(log_dir)

    # This needs to be done here because daemonic processes can't have children
    env = make_envs(env_id, n_envs, seed)

    def a2c():
        if make_reward_predictor:
            reward_predictor = make_reward_predictor('a2c', cluster_dict)
        else:
            reward_predictor = None
        learn(
            policy=policy_fn,
            env=env,
            seed=seed,
            seg_pipe=seg_pipe,
            go_pipe=go_pipe,
            log_dir=log_dir,
            lr_scheduler=lr_scheduler,
            total_timesteps=num_timesteps,
            load_path=ckpt_dir,
            reward_predictor=reward_predictor,
            episode_vid_queue=episode_vid_queue,
            ent_coef=ent_coef,
            save_interval=ckpt_interval,
            gen_segments=gen_segments)
    a2c_proc = Process(target=a2c, daemon=True)
    a2c_proc.start()
    return env, a2c_proc

def start_pref_interface(seg_pipe, pref_pipe, segs_max, headless):
    def pi_procf(pi):
        # TODO hack
        sys.stdin = os.fdopen(0)
        pi.run(seg_pipe, pref_pipe, segs_max)
    pi = PrefInterface(headless=headless, synthetic_prefs=headless)
    pi_proc = Process(target=pi_procf, daemon=True, args=(pi, ))
    pi_proc.start()
    return pi_proc


def start_reward_predictor_training(cluster_dict, make_reward_predictor, just_pretrain, pref_pipe,
                                    go_pipe, db_max, n_initial_epochs,
                                    prefs_dir, ckpt_path, val_interval, ckpt_interval):
    def trp():
        reward_predictor = make_reward_predictor('train', cluster_dict)
        reward_predictor.init_network(ckpt_path)

        if prefs_dir is not None:
            pref_db_train, pref_db_val = load_pref_db(prefs_dir)
        else:
            pref_db_train, pref_db_val = pref_interface.get_initial_prefs(pref_pipe=pref_pipe,
                                                                          n_initial_prefs=n_initial_epochs,
                                                                          db_max=db_max)

        print("Pretraining reward predictor for {} epochs".format(n_initial_epochs))
        for i in range(n_initial_epochs):
            print("Epoch {}".format(i))
            reward_predictor.train(pref_db_train, pref_db_val, val_interval)

            if i and i % ckpt_interval == 0:
                reward_predictor.save()
        print("Reward predictor pretraining done")

        if just_pretrain:
            return

        go_pipe.put(True)

        while True:
            reward_predictor.train(pref_db_train, pref_db_val, val_interval)
            if i and i % ckpt_interval == 0:
                reward_predictor.save()
            recv_prefs(pref_pipe, pref_db_train, pref_db_val, db_max)

    train_proc = Process(target=trp, daemon=True)
    train_proc.start()
    return train_proc


def start_episode_renderer():
    def episode_vid_proc():
        vid_proc(
            episode_vid_queue,
            playback_speed=2,
            zoom_factor=2,
            mode='play_through')
    episode_vid_queue = Queue()
    Process(
        target=episode_vid_proc,
        daemon=True).start()
    return episode_vid_queue


def make_envs(env_id, n_envs, seed):
    def make_env(rank):
        def _thunk():
            env = gym.make(env_id)
            env.seed(seed + rank)
            # TODO
            if env_id == 'EnduroNoFrameskip-v4':
                env = EnduroWrapper(env)
            gym.logger.setLevel(logging.WARN)
            return wrap_deepmind_nomax(env)
        return _thunk

    set_global_seeds(seed)
    env = SubprocVecEnv(env_id, [make_env(i) for i in range(n_envs)])

    return env


def main():
    general_args, a2c_args, pref_interface_args, reward_predictor_training_args = parse_args()

    misc_logs_dir = osp.join(general_args['log_dir'], 'misc')
    os.makedirs(misc_logs_dir)
    easy_tf_log.set_dir(misc_logs_dir)

    run(general_args=general_args, a2c_args=a2c_args,
        pref_interface_args=pref_interface_args, reward_predictor_training_args=reward_predictor_training_args)

if __name__ == '__main__':
    main()
