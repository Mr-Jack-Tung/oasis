from __future__ import annotations

import argparse
import asyncio
from genericpath import isfile
import json
import logging
import os
from datetime import datetime, timedelta
import random
from typing import Any

from colorama import Back
from yaml import safe_load

from social_simulation.clock.clock import Clock
from social_simulation.inference.inference_manager import InferencerManager
from social_simulation.social_agent.agents_generator import (
    gen_control_agents_with_data, generate_reddit_agents)
from social_simulation.social_platform.channel import Channel
from social_simulation.social_platform.platform import Platform
from social_simulation.social_platform.typing import ActionType
from social_simulation.testing.show_db import print_db_contents


social_log = logging.getLogger(name='social')
social_log.setLevel('DEBUG')
now = datetime.now()
file_handler = logging.FileHandler(f'./log/social-{str(now)}.log', encoding='utf-8')
file_handler.setLevel('DEBUG')
file_handler.setFormatter(logging.Formatter('%(levelname)s - %(asctime)s - %(name)s - %(message)s'))
social_log.addHandler(file_handler)
stream_handler = logging.StreamHandler()
stream_handler.setLevel('DEBUG')
stream_handler.setFormatter(logging.Formatter('%(levelname)s - %(asctime)s - %(name)s - %(message)s'))
social_log.addHandler(stream_handler)

parser = argparse.ArgumentParser(description="Arguments for script.")
parser.add_argument(
    "--config_path",
    type=str,
    help="Path to the YAML config file.",
    required=False,
    default="",
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "mock_reddit.db")
DEFAULT_USER_PATH = os.path.join(DATA_DIR, "reddit",
                                 "filter_user_results.json")
DEFAULT_PAIR_PATH = os.path.join(DATA_DIR, "reddit", "RS-RC-pairs.json")
DEFAULT_EXP_PATH = os.path.join(DATA_DIR, "reddit", "exp_info.json")

ROUND_POST_NUM = 20


async def running(
    db_path: str | None = DEFAULT_DB_PATH,
    user_path: str | None = DEFAULT_USER_PATH,
    pair_path: str | None = DEFAULT_PAIR_PATH,
    exp_info_filename: str | None = DEFAULT_EXP_PATH,
    round_post_num: str | None = ROUND_POST_NUM,
    num_timesteps: int = 3,
    clock_factor: int = 60,
    recsys_type: str = "reddit",
    controllable_user: bool = True,
    allow_self_rating: bool = False,
    show_score: bool = True,
    max_rec_post_len: int = 20,
    activate_prob: float = 0.1,
    follow_post_agent: bool = False,
    mute_post_agent: bool = True,
    model_configs: dict[str, Any] | None = None,
    inference_configs: dict[str, Any] | None = None,
    init_comment_score: int = 0
) -> None:
    db_path = DEFAULT_DB_PATH if db_path is None else db_path
    user_path = DEFAULT_USER_PATH if user_path is None else user_path
    pair_path = DEFAULT_PAIR_PATH if pair_path is None else pair_path
    exp_info_filename = DEFAULT_EXP_PATH if exp_info_filename is None else exp_info_filename
    if os.path.exists(db_path):
        os.remove(db_path)

    start_time = datetime(2024, 8, 6, 8, 0)
    clock = Clock(k=clock_factor)
    twitter_channel = Channel()
    infra = Platform(
        db_path,
        twitter_channel,
        clock,
        start_time,
        allow_self_rating=allow_self_rating,
        show_score=show_score,
        recsys_type=recsys_type,
        max_rec_post_len=max_rec_post_len
    )
    inference_channel = Channel()
    print('inference_configs:', inference_configs)
    infere = InferencerManager(
        inference_channel,
        **inference_configs,
    )
    twitter_task = asyncio.create_task(infra.running())
    inference_task = asyncio.create_task(infere.run())

    if not controllable_user:
        raise ValueError("Uncontrollable user is not supported")
    else:
        agent_graph, agent_user_id_mapping = \
            await gen_control_agents_with_data(
                twitter_channel,
                2,
            )
        agent_graph = await generate_reddit_agents(
            user_path,
            twitter_channel,
            inference_channel,
            agent_graph,
            agent_user_id_mapping,
            follow_post_agent,
            mute_post_agent,
            **model_configs,
        )
    with open(pair_path, "r") as f:
        pairs = json.load(f)

    exp_info = {
        "up_comment_id": [],
        "down_comment_id": [],
        "control_comment_id": []
    }

    for timestep in range(num_timesteps):
        os.environ['TIME_STAMP'] = str(timestep+1)
        if timestep == 0:
            start_time_0 = datetime.now()
        # print(Back.GREEN + f"timestep:{timestep}" + Back.RESET)
        social_log.info(f"timestep:{timestep + 1}.")
        
        post_agent = agent_graph.get_agent(0)
        rate_agent = agent_graph.get_agent(1)

        async def export_data(i):
            rs_rc_index = i + timestep * round_post_num
            if rs_rc_index >= len(pairs):
                return
            else:
                content = pairs[rs_rc_index]["RC_1"]["body"]
                response = await post_agent.perform_action_by_data(
                    'create_post', content=content)
                post_id = response['post_id']
                # for i in range(1, 11):
                #     key_name = f"RC_{i}"
                #     if key_name not in pairs[rs_rc_index]:
                #         break
                #     response = await post_agent.perform_action_by_data(
                #         'create_comment',
                #         post_id=post_id,
                #         content=pairs[rs_rc_index][key_name]["body"])
                #     comment_id = response['comment_id']

                    # if pairs[rs_rc_index][key_name]["group"] == 'up':
                    #     await rate_agent.perform_action_by_data(
                    #         'like_comment', comment_id)
                    #     exp_info['up_comment_id'].append(comment_id)
                    # elif pairs[rs_rc_index][key_name]["group"] == 'down':
                    #     await rate_agent.perform_action_by_data(
                    #         'dislike_comment', comment_id)
                    #     exp_info['down_comment_id'].append(comment_id)
                    # elif pairs[rs_rc_index][key_name]["group"] == 'control':
                    #     exp_info['control_comment_id'].append(comment_id)
                    # else:
                    #     raise ValueError("Unsupported value of 'group'")
                if init_comment_score == 1:
                    await rate_agent.perform_action_by_data(
                        'like', post_id)
                elif init_comment_score == -1:
                    await rate_agent.perform_action_by_data(
                        'dislike', post_id)
                elif init_comment_score == 0:
                    pass
                else:
                    raise ValueError(f"Unsupported value of init_comment_score: {init_comment_score}")

        tasks = [export_data(i) for i in range(round_post_num)]
        await asyncio.gather(*tasks)
        await infra.update_rec_table()
        social_log.info("update rec table.")
        tasks = []
        for _, agent in agent_graph.get_agents():
            if agent.user_info.is_controllable is False:
                if random.random() < activate_prob:
                    tasks.append(agent.perform_action_by_llm())
        random.shuffle(tasks)
        await asyncio.gather(*tasks)

        if timestep == 0:
            time_difference = datetime.now() - start_time_0

            # 将两个小时转换为秒，因为time_difference是一个timedelta对象
            two_hours_in_seconds = timedelta(hours=2).total_seconds()

            # 计算两个小时除以时间差（以秒为单位）
            clock_factor = two_hours_in_seconds / time_difference.total_seconds()
            clock.k = clock_factor
            social_log.info(f'clock_factor: {clock_factor}')


    await twitter_channel.write_to_receive_queue((None, None, ActionType.EXIT))
    await infere.stop()
    await twitter_task, inference_task

    with open(exp_info_filename, 'w') as f:
        json.dump(exp_info, f, indent=4)
    social_log.info("Simulation finish!")


if __name__ == "__main__":
    args = parser.parse_args()
  
    if os.path.exists(args.config_path):
        with open(args.config_path, "r") as f:
            cfg = safe_load(f)
        data_params = cfg.get("data")
        simulation_params = cfg.get("simulation")
        model_configs = cfg.get("model")
        inference_params = cfg.get("inference")

        asyncio.run(
            running(
                **data_params,
                **simulation_params,
                model_configs=model_configs,
                inference_configs=inference_params
            ), debug=True)
    else:
        asyncio.run(running())
