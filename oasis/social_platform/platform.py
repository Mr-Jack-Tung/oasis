from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any

from oasis.clock.clock import Clock
from oasis.social_platform.database import (create_db,
                                            fetch_rec_table_as_matrix,
                                            fetch_table_from_db)
from oasis.social_platform.platform_utils import PlatformUtils
from oasis.social_platform.recsys import (rec_sys_personalized_twh,
                                          rec_sys_personalized_with_trace,
                                          rec_sys_random, rec_sys_reddit)
from oasis.social_platform.typing import ActionType, RecsysType

if "sphinx" not in sys.modules:
    twitter_log = logging.getLogger(name="social.twitter")
    twitter_log.setLevel("DEBUG")
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_handler = logging.FileHandler(f"./log/social.twitter-{now}.log")
    file_handler.setLevel("DEBUG")
    file_handler.setFormatter(
        logging.Formatter(
            "%(levelname)s - %(asctime)s - %(name)s - %(message)s"))
    twitter_log.addHandler(file_handler)


class Platform:
    r"""Platform."""

    def __init__(
        self,
        db_path: str,
        channel: Any,
        sandbox_clock: Clock | None = None,
        start_time: datetime | None = None,
        show_score: bool = False,
        allow_self_rating: bool = True,
        recsys_type: str | RecsysType = "reddit",
        refresh_rec_post_count: int = 1,
        max_rec_post_len: int = 2,
        following_post_count=3,
    ):
        self.db_path = db_path
        self.recsys_type = recsys_type
        # import pdb; pdb.set_trace()
        if self.recsys_type == "reddit":
            # 未指定时钟时，默认platform的时间放大系数为60
            if sandbox_clock is None:
                sandbox_clock = Clock(60)
            if start_time is None:
                start_time = datetime.now()
            self.start_time = start_time
            self.sandbox_clock = sandbox_clock
        else:
            self.start_time = 0
            self.sandbox_clock = None

        self.db, self.db_cursor = create_db(self.db_path)
        self.db.execute("PRAGMA synchronous = OFF")

        self.channel = channel

        self.recsys_type = RecsysType(recsys_type)

        # 是否要模拟显示类似reddit的那种点赞数减去点踩数作为分数
        # 而不分别显示点赞数和点踩数
        self.show_score = show_score

        # 是否允许用户给自己的post和comment点赞或者点踩
        self.allow_self_rating = allow_self_rating

        # 社交媒体内部推荐系统refresh一次返回的推文数量
        self.refresh_rec_post_count = refresh_rec_post_count
        # 从关注用户发出post中根据like数排行一次返回的推文数量
        self.following_post_count = following_post_count
        # rec table(buffer)中每个用户的最大post数量
        self.max_rec_post_len = max_rec_post_len
        # rec prob between random and personalized
        self.rec_prob = 0.7

        # platform内部定义的热搜规则参数
        self.trend_num_days = 7
        self.trend_top_k = 1

        self.pl_utils = PlatformUtils(
            self.db,
            self.db_cursor,
            self.start_time,
            self.sandbox_clock,
            self.show_score,
        )

    async def running(self):
        while True:
            message_id, data = await self.channel.receive_from()

            agent_id, message, action = data
            action = ActionType(action)

            if action == ActionType.EXIT:
                if self.db_path == ":memory:":
                    dst = sqlite3.connect("mock.db")
                    with dst:
                        self.db.backup(dst)

                self.db_cursor.close()
                self.db.close()
                break

            # 通过getattr获取相应的函数
            action_function = getattr(self, action.value, None)
            if action_function:
                # 获取函数的参数名称
                func_code = action_function.__code__
                param_names = func_code.co_varnames[:func_code.co_argcount]

                len_param_names = len(param_names)
                if len_param_names > 3:
                    raise ValueError(
                        f"Functions with {len_param_names} parameters are not "
                        f"supported.")
                # 构建参数字典
                params = {}
                if len_param_names >= 2:
                    params["agent_id"] = agent_id
                if len_param_names == 3:
                    # 假设param_names中第二个元素是你想要添加的第二个参数名称
                    second_param_name = param_names[2]
                    params[second_param_name] = message

                # 调用函数并传入参数
                result = await action_function(**params)
                await self.channel.send_to((message_id, agent_id, result))
            else:
                raise ValueError(f"Action {action} is not supported")

    def run(self):
        asyncio.run(self.running())

    # 注册
    async def sign_up(self, agent_id, user_message):
        user_name, name, bio = user_message
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            # 插入用户记录
            user_insert_query = (
                "INSERT INTO user (user_id, agent_id, user_name, name, bio, "
                "created_at, num_followings, num_followers) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)")
            self.pl_utils._execute_db_command(
                user_insert_query,
                (agent_id, agent_id, user_name, name, bio, current_time, 0, 0),
                commit=True,
            )
            user_id = agent_id
            # 准备trace记录的信息
            action_info = {"name": name, "user_name": user_name, "bio": bio}
            self.pl_utils._record_trace(user_id, ActionType.SIGNUP.value,
                                        action_info, current_time)
            twitter_log.info(f"Trace inserted: user_id={user_id}, "
                             f"current_time={current_time}, "
                             f"action={ActionType.SIGNUP.value}, "
                             f"info={action_info}")
            return {"success": True, "user_id": user_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def refresh(self, agent_id: int):
        # output不变，执行内容是从rec table取特定id的tweet
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # 从rec表中获取指定user_id的所有post_id
            rec_query = "SELECT post_id FROM rec WHERE user_id = ?"
            self.pl_utils._execute_db_command(rec_query, (user_id, ))
            rec_results = self.db_cursor.fetchall()

            post_ids = [row[0] for row in rec_results]
            selected_post_ids = post_ids
            # 如果post_id数量 >= self.refresh_rec_post_count，则随机选择指定数量的post_id
            if len(selected_post_ids) >= self.refresh_rec_post_count:
                selected_post_ids = random.sample(selected_post_ids,
                                                  self.refresh_rec_post_count)

            if self.recsys_type != RecsysType.REDDIT:
                # 从following中去获取post (in network)
                # 更改SQL查询，令refresh得到的 post 是这个用户关注的人的 post，排序按照推特的点赞数
                query_following_post = (
                    "SELECT post.post_id, post.user_id, post.content, "
                    "post.created_at, post.num_likes FROM post "
                    "JOIN follow ON post.user_id = follow.followee_id "
                    "WHERE follow.follower_id = ? "
                    "ORDER BY post.num_likes DESC  "
                    "LIMIT ?")
                self.pl_utils._execute_db_command(
                    query_following_post,
                    (
                        user_id,
                        self.following_post_count,
                    ),
                )

                following_posts = self.db_cursor.fetchall()
                following_posts_ids = [row[0] for row in following_posts]

                selected_post_ids = following_posts_ids + selected_post_ids
                selected_post_ids = list(set(selected_post_ids))

            # 根据选定的post_id从post表中获取post详情
            placeholders = ", ".join("?" for _ in selected_post_ids)
            # 构造SQL查询字符串
            post_query = (
                f"SELECT post_id, user_id, content, created_at, num_likes, "
                f"num_dislikes FROM post WHERE post_id IN ({placeholders})")
            self.pl_utils._execute_db_command(post_query, selected_post_ids)
            results = self.db_cursor.fetchall()
            if not results:
                return {"success": False, "message": "No posts found."}
            results_with_comments = self.pl_utils._add_comments_to_posts(
                results)
            # 记录操作到trace表
            action_info = {"posts": results_with_comments}
            twitter_log.info(action_info)
            self.pl_utils._record_trace(user_id, ActionType.REFRESH.value,
                                        action_info, current_time)

            return {"success": True, "posts": results_with_comments}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def update_rec_table(self):
        # Recsys(trace/user/post table), 结果是刷新了rec table
        user_table = fetch_table_from_db(self.db_cursor, "user")
        post_table = fetch_table_from_db(self.db_cursor, "post")
        trace_table = fetch_table_from_db(self.db_cursor, "trace")
        rec_matrix = fetch_rec_table_as_matrix(self.db_cursor)

        if self.recsys_type == RecsysType.RANDOM:
            new_rec_matrix = rec_sys_random(user_table, post_table,
                                            trace_table, rec_matrix,
                                            self.max_rec_post_len)
        elif self.recsys_type == RecsysType.TWITTER:
            new_rec_matrix = rec_sys_personalized_with_trace(
                user_table, post_table, trace_table, rec_matrix,
                self.max_rec_post_len)
        elif self.recsys_type == RecsysType.TWHIN:
            latest_post_time = post_table[-1]["created_at"]
            post_query = "SELECT COUNT(*) " "FROM post " "WHERE created_at = ?"

            # 得到新发出的post条数，从而进行逐步更新
            self.pl_utils._execute_db_command(post_query, (latest_post_time, ))
            result = self.db_cursor.fetchone()
            latest_post_count = result[0]
            if not latest_post_count:
                return {
                    "success": False,
                    "message": "Fail to get latest posts count"
                }
            new_rec_matrix = rec_sys_personalized_twh(
                user_table,
                post_table,
                latest_post_count,
                trace_table,
                rec_matrix,
                self.max_rec_post_len,
            )
        elif self.recsys_type == RecsysType.REDDIT:
            new_rec_matrix = rec_sys_reddit(post_table, rec_matrix,
                                            self.max_rec_post_len)
        else:
            raise ValueError("Unsupported recommendation system type, please "
                             "check the `RecsysType`.")

        # 构建SQL语句以删除rec表中的所有记录
        sql_query = "DELETE FROM rec"
        # 使用封装好的_execute_db_command函数执行SQL语句
        self.pl_utils._execute_db_command(sql_query, commit=True)

        # 批量插入更省时, 创建插入值列表
        insert_values = [(user_id, post_id)
                         for user_id in range(len(new_rec_matrix))
                         for post_id in new_rec_matrix[user_id]]

        # 批量插入到数据库
        self.pl_utils._execute_many_db_command(
            "INSERT INTO rec (user_id, post_id) VALUES (?, ?)",
            insert_values,
            commit=True,
        )

    async def create_post(self, agent_id: int, content: str):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id

            # 插入推文记录
            post_insert_query = (
                "INSERT INTO post (user_id, content, created_at, num_likes, "
                "num_dislikes) VALUES (?, ?, ?, ?, ?)")
            self.pl_utils._execute_db_command(
                post_insert_query, (user_id, content, current_time, 0, 0),
                commit=True)
            post_id = self.db_cursor.lastrowid
            # 准备trace记录的信息
            action_info = {"content": content, "post_id": post_id}
            self.pl_utils._record_trace(user_id, ActionType.CREATE_POST.value,
                                        action_info, current_time)
            twitter_log.info(f"Trace inserted: user_id={user_id}, "
                             f"current_time={current_time}, "
                             f"action={ActionType.CREATE_POST.value}, "
                             f"info={action_info}")
            return {"success": True, "post_id": post_id}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def repost(self, agent_id: int, post_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id

            # 查询要转发的推特内容
            sql_query = (
                "SELECT post_id, user_id, content, created_at, num_likes "
                "FROM post "
                "WHERE post_id = ? ")
            # 执行数据库查询
            self.pl_utils._execute_db_command(sql_query, (post_id, ))
            results = self.db_cursor.fetchall()
            if not results:
                return {"success": False, "error": "Post not found."}

            prev_content = results[0][2]
            if "original_post: " in prev_content:
                orig_content = prev_content.split("original_post: ")[-1]
            else:
                orig_content = prev_content
            orig_content = f"%{orig_content}%"
            prev_like = results[0][-1]
            prev_user_id = results[0][1]

            # 转发的推特标识一下是从哪个user转的，方便判断
            repost_content = (
                f"user{user_id} repost from user{str(prev_user_id)}. "
                f"original_post: {prev_content}")

            # 确保相关内容此前未被该用户转发过
            repost_check_query = (
                "SELECT * FROM 'post' WHERE content LIKE ? AND user_id = ?")
            self.pl_utils._execute_db_command(repost_check_query,
                                              (orig_content, user_id))
            if self.db_cursor.fetchone():
                # 该用户存在转发记录
                return {
                    "success": False,
                    "error": "Repost record already exists."
                }

            # 插入转推推文记录
            post_insert_query = (
                "INSERT INTO post (user_id, content, created_at, num_likes) "
                "VALUES (?, ?, ?, ?)")

            self.pl_utils._execute_db_command(
                post_insert_query,
                (user_id, repost_content, current_time, prev_like),
                commit=True,
            )

            post_id = self.db_cursor.lastrowid
            # 准备trace记录的信息
            action_info = {"post_id": post_id}
            self.pl_utils._record_trace(user_id, ActionType.REPOST.value,
                                        action_info, current_time)

            return {"success": True, "post_id": post_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def like_post(self, agent_id: int, post_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # 检查是否已经存在点赞记录
            like_check_query = ("SELECT * FROM 'like' WHERE post_id = ? AND "
                                "user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (post_id, user_id))
            if self.db_cursor.fetchone():
                # 已存在点赞记录
                return {
                    "success": False,
                    "error": "Like record already exists."
                }

            # 检查要点赞的推文是否是自己发布的
            if self.allow_self_rating is False:
                check_result = self.pl_utils._check_self_post_rating(
                    post_id, user_id)
                if check_result:
                    return check_result

            # 更新post表中的点赞数
            post_update_query = (
                "UPDATE post SET num_likes = num_likes + 1 WHERE post_id = ?")
            self.pl_utils._execute_db_command(post_update_query, (post_id, ),
                                              commit=True)

            # 在like表中添加记录
            like_insert_query = (
                "INSERT INTO 'like' (post_id, user_id, created_at) "
                "VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(like_insert_query,
                                              (post_id, user_id, current_time),
                                              commit=True)
            like_id = self.db_cursor.lastrowid  # 获取刚刚插入的点赞记录的ID

            # 记录操作到trace表
            action_info = {"post_id": post_id, "like_id": like_id}
            self.pl_utils._record_trace(user_id, ActionType.LIKE_POST.value,
                                        action_info, current_time)
            return {"success": True, "like_id": like_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def unlike_post(self, agent_id: int, post_id: int):
        try:
            user_id = agent_id

            # 检查是否已经存在点赞记录
            like_check_query = ("SELECT * FROM 'like' WHERE post_id = ? AND "
                                "user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (post_id, user_id))
            result = self.db_cursor.fetchone()

            if not result:
                # 没有存在点赞记录
                return {
                    "success": False,
                    "error": "Like record does not exist."
                }

            # Get the `like_id`
            like_id, _, _, _ = result

            # 更新post表中的点赞数
            post_update_query = (
                "UPDATE post SET num_likes = num_likes - 1 WHERE post_id = ?")
            self.pl_utils._execute_db_command(
                post_update_query,
                (post_id, ),
                commit=True,
            )

            # 在like表中删除记录
            like_delete_query = "DELETE FROM 'like' WHERE like_id = ?"
            self.pl_utils._execute_db_command(
                like_delete_query,
                (like_id, ),
                commit=True,
            )

            # 记录操作到trace表
            action_info = {"post_id": post_id, "like_id": like_id}
            self.pl_utils._record_trace(user_id, ActionType.UNLIKE_POST.value,
                                        action_info)
            return {"success": True, "like_id": like_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def dislike_post(self, agent_id: int, post_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # 检查是否已经存在dislike记录
            like_check_query = (
                "SELECT * FROM 'dislike' WHERE post_id = ? AND user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (post_id, user_id))
            if self.db_cursor.fetchone():
                # 已存在点赞记录
                return {
                    "success": False,
                    "error": "Dislike record already exists."
                }

            # 检查要点踩的推文是否是自己发布的
            if self.allow_self_rating is False:
                check_result = self.pl_utils._check_self_post_rating(
                    post_id, user_id)
                if check_result:
                    return check_result

            # 更新post表中的dislike数
            post_update_query = (
                "UPDATE post SET num_dislikes = num_dislikes + 1 WHERE "
                "post_id = ?")
            self.pl_utils._execute_db_command(post_update_query, (post_id, ),
                                              commit=True)

            # 在dislike表中添加记录
            dislike_insert_query = (
                "INSERT INTO 'dislike' (post_id, user_id, created_at) "
                "VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(dislike_insert_query,
                                              (post_id, user_id, current_time),
                                              commit=True)
            dislike_id = self.db_cursor.lastrowid  # 获取刚刚插入的点赞记录的ID

            # 记录操作到trace表
            action_info = {"post_id": post_id, "dislike_id": dislike_id}
            self.pl_utils._record_trace(user_id, ActionType.DISLIKE_POST.value,
                                        action_info, current_time)
            return {"success": True, "dislike_id": dislike_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def undo_dislike_post(self, agent_id: int, post_id: int):
        try:
            user_id = agent_id

            # 检查是否已经存在dislike记录
            like_check_query = (
                "SELECT * FROM 'dislike' WHERE post_id = ? AND user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (post_id, user_id))
            result = self.db_cursor.fetchone()

            if not result:
                # 没有存在dislike记录
                return {
                    "success": False,
                    "error": "Dislike record does not exist."
                }

            # Get the `dislike_id`
            dislike_id, _, _, _ = result

            # 更新post表中的点踩数
            post_update_query = (
                "UPDATE post SET num_dislikes = num_dislikes - 1 WHERE "
                "post_id = ?")
            self.pl_utils._execute_db_command(
                post_update_query,
                (post_id, ),
                commit=True,
            )

            # 在dislike表中删除记录
            like_delete_query = "DELETE FROM 'dislike' WHERE dislike_id = ?"
            self.pl_utils._execute_db_command(
                like_delete_query,
                (dislike_id, ),
                commit=True,
            )

            # 记录操作到trace表
            action_info = {"post_id": post_id, "dislike_id": dislike_id}
            self.pl_utils._record_trace(user_id,
                                        ActionType.UNDO_DISLIKE_POST.value,
                                        action_info)
            return {"success": True, "dislike_id": dislike_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search_posts(self, agent_id: int, query: str):
        try:
            user_id = agent_id
            # 更新SQL查询，以便同时根据content、post_id和user_id进行搜索
            # 注意：CAST是必要的，因为post_id和user_id是整数类型，而搜索的query是字符串类型
            sql_query = (
                "SELECT post_id, user_id, content, created_at, num_likes, "
                "num_dislikes FROM post "
                "WHERE content LIKE ? OR CAST(post_id AS TEXT) LIKE ? OR "
                "CAST(user_id AS TEXT) LIKE ?")
            # 执行数据库查询
            self.pl_utils._execute_db_command(
                sql_query,
                ("%" + query + "%", "%" + query + "%", "%" + query + "%"),
                commit=True,
            )
            results = self.db_cursor.fetchall()

            # 记录操作到trace表
            action_info = {"query": query}
            self.pl_utils._record_trace(user_id, ActionType.SEARCH_POSTS.value,
                                        action_info)

            # 如果没有找到结果，返回一个指示失败的字典
            if not results:
                return {
                    "success": False,
                    "message": "No posts found matching the query.",
                }
            results_with_comments = self.pl_utils._add_comments_to_posts(
                results)

            return {"success": True, "posts": results_with_comments}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search_user(self, agent_id: int, query: str):
        try:
            user_id = agent_id
            sql_query = (
                "SELECT user_id, user_name, name, bio, created_at, "
                "num_followings, num_followers "
                "FROM user "
                "WHERE user_name LIKE ? OR name LIKE ? OR bio LIKE ? OR "
                "CAST(user_id AS TEXT) LIKE ?")
            # 改写为使用 execute_db_command 方法
            self.pl_utils._execute_db_command(
                sql_query,
                (
                    "%" + query + "%",
                    "%" + query + "%",
                    "%" + query + "%",
                    "%" + query + "%",
                ),
                commit=True,
            )
            results = self.db_cursor.fetchall()

            # 记录操作到trace表
            action_info = {"query": query}
            self.pl_utils._record_trace(user_id, ActionType.SEARCH_USER.value,
                                        action_info)

            # If no results found, return a dict with 'success' key as False:
            if not results:
                return {
                    "success": False,
                    "message": "No users found matching the query.",
                }

            # Convert each tuple in results to a dictionary:
            users = [{
                "user_id": user_id,
                "user_name": user_name,
                "name": name,
                "bio": bio,
                "created_at": created_at,
                "num_followings": num_followings,
                "num_followers": num_followers,
            } for user_id, user_name, name, bio, created_at, num_followings,
                     num_followers in results]
            return {"success": True, "users": users}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def follow(self, agent_id: int, followee_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # # 检查是否已经存在关注记录
            follow_check_query = ("SELECT * FROM follow WHERE follower_id = ? "
                                  "AND followee_id = ?")
            self.pl_utils._execute_db_command(follow_check_query,
                                              (user_id, followee_id))
            if self.db_cursor.fetchone():
                # 已存在关注记录
                return {
                    "success": False,
                    "error": "Follow record already exists."
                }

            # 在follow表中添加记录
            follow_insert_query = (
                "INSERT INTO follow (follower_id, followee_id, created_at) "
                "VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(
                follow_insert_query, (user_id, followee_id, current_time),
                commit=True)
            follow_id = self.db_cursor.lastrowid  # 获取刚刚插入的关注记录的ID

            # 更新user表中的following字段
            user_update_query1 = (
                "UPDATE user SET num_followings = num_followings + 1 "
                "WHERE user_id = ?")
            self.pl_utils._execute_db_command(user_update_query1, (user_id, ),
                                              commit=True)

            # 更新user表中的follower字段
            user_update_query2 = (
                "UPDATE user SET num_followers = num_followers + 1 "
                "WHERE user_id = ?")
            self.pl_utils._execute_db_command(user_update_query2,
                                              (followee_id, ),
                                              commit=True)

            # 记录操作到trace表
            action_info = {"follow_id": follow_id}
            self.pl_utils._record_trace(user_id, ActionType.FOLLOW.value,
                                        action_info, current_time)
            twitter_log.info(f"Trace inserted: user_id={user_id}, "
                             f"current_time={current_time}, "
                             f"action={ActionType.FOLLOW.value}, "
                             f"info={action_info}")
            return {"success": True, "follow_id": follow_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def unfollow(self, agent_id: int, followee_id: int):
        try:
            user_id = agent_id
            # 检查是否存在关注记录，并获取其ID
            follow_check_query = (
                "SELECT follow_id FROM follow WHERE follower_id = ? AND "
                "followee_id = ?")
            self.pl_utils._execute_db_command(follow_check_query,
                                              (user_id, followee_id))
            follow_record = self.db_cursor.fetchone()
            if not follow_record:
                return {
                    "success": False,
                    "error": "Follow record does not exist."
                }
            follow_id = follow_record[0]  # 假设ID位于查询结果的第一列

            # 在follow表中删除记录
            follow_delete_query = "DELETE FROM follow WHERE follow_id = ?"
            self.pl_utils._execute_db_command(follow_delete_query,
                                              (follow_id, ),
                                              commit=True)

            # 更新user表中的following字段
            user_update_query1 = (
                "UPDATE user SET num_followings = num_followings - 1 "
                "WHERE user_id = ?")
            self.pl_utils._execute_db_command(user_update_query1, (user_id, ),
                                              commit=True)

            # 更新user表中的follower字段
            user_update_query2 = (
                "UPDATE user SET num_followers = num_followers - 1 "
                "WHERE user_id = ?")
            self.pl_utils._execute_db_command(user_update_query2,
                                              (followee_id, ),
                                              commit=True)

            # 记录操作到trace表
            action_info = {"followee_id": followee_id}
            self.pl_utils._record_trace(user_id, ActionType.UNFOLLOW.value,
                                        action_info)
            return {
                "success": True,
                "follow_id": follow_id,  # 返回被删除的关注记录ID
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mute(self, agent_id: int, mutee_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # 检查是否已经存在禁言记录
            mute_check_query = ("SELECT * FROM mute WHERE muter_id = ? AND "
                                "mutee_id = ?")
            self.pl_utils._execute_db_command(mute_check_query,
                                              (user_id, mutee_id))
            if self.db_cursor.fetchone():
                # 已存在禁言记录
                return {
                    "success": False,
                    "error": "Mute record already exists."
                }
            # 在mute表中添加记录
            mute_insert_query = (
                "INSERT INTO mute (muter_id, mutee_id, created_at) "
                "VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(
                mute_insert_query, (user_id, mutee_id, current_time),
                commit=True)
            mute_id = self.db_cursor.lastrowid  # 获取刚刚插入的禁言记录的ID

            # 记录操作到trace表
            action_info = {"mutee_id": mutee_id}
            self.pl_utils._record_trace(user_id, ActionType.MUTE.value,
                                        action_info, current_time)
            return {"success": True, "mute_id": mute_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def unmute(self, agent_id: int, mutee_id: int):
        try:
            user_id = agent_id
            # 检查是否存在指定的禁言记录，并获取mute_id
            mute_check_query = (
                "SELECT mute_id FROM mute WHERE muter_id = ? AND mutee_id = ?")
            self.pl_utils._execute_db_command(mute_check_query,
                                              (user_id, mutee_id))
            mute_record = self.db_cursor.fetchone()
            if not mute_record:
                # 如果不存在禁言记录
                return {"success": False, "error": "No mute record exists."}
            mute_id = mute_record[0]

            # 从mute表中删除指定的禁言记录
            mute_delete_query = "DELETE FROM mute WHERE mute_id = ?"
            self.pl_utils._execute_db_command(mute_delete_query, (mute_id, ),
                                              commit=True)

            # 记录解除禁言操作到trace表
            action_info = {"mutee_id": mutee_id}
            self.pl_utils._record_trace(user_id, ActionType.UNMUTE.value,
                                        action_info)
            return {"success": True, "mute_id": mute_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def trend(self, agent_id: int):
        """
        Get the top K trending posts in the last num_days days.
        """
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id
            # 计算搜索的起始时间
            if self.recsys_type == RecsysType.REDDIT:
                start_time = current_time - timedelta(days=self.trend_num_days)
            else:
                start_time = int(current_time) - self.trend_num_days * 24 * 60

            # 构建SQL查询语句
            sql_query = """
                SELECT user_id, post_id, content, created_at, num_likes,
                num_dislikes FROM post
                WHERE created_at >= ?
                ORDER BY num_likes DESC
                LIMIT ?
            """
            # 执行数据库查询
            self.pl_utils._execute_db_command(sql_query,
                                              (start_time, self.trend_top_k),
                                              commit=True)
            results = self.db_cursor.fetchall()

            # 如果没有找到结果，返回一个指示失败的字典
            if not results:
                return {
                    "success": False,
                    "message": "No trending posts in the specified period.",
                }
            results_with_comments = self.pl_utils._add_comments_to_posts(
                results)

            action_info = {"posts": results_with_comments}
            self.pl_utils._record_trace(user_id, ActionType.TREND.value,
                                        action_info, current_time)

            return {"success": True, "posts": results_with_comments}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_comment(self, agent_id: int, comment_message: tuple):
        post_id, content = comment_message
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id

            # 插入评论记录
            comment_insert_query = (
                "INSERT INTO comment (post_id, user_id, content, created_at) "
                "VALUES (?, ?, ?, ?)")
            self.pl_utils._execute_db_command(
                comment_insert_query,
                (post_id, user_id, content, current_time),
                commit=True,
            )
            comment_id = self.db_cursor.lastrowid

            # 准备trace记录的信息
            action_info = {"content": content, "comment_id": comment_id}
            self.pl_utils._record_trace(user_id,
                                        ActionType.CREATE_COMMENT.value,
                                        action_info, current_time)

            return {"success": True, "comment_id": comment_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def like_comment(self, agent_id: int, comment_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id

            # 检查是否已经存在点赞记录
            like_check_query = (
                "SELECT * FROM comment_like WHERE comment_id = ? AND "
                "user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (comment_id, user_id))
            if self.db_cursor.fetchone():
                # 已存在点赞记录
                return {
                    "success": False,
                    "error": "Comment like record already exists.",
                }

            # 检查要点赞的评论是否是自己发布的
            if self.allow_self_rating is False:
                check_result = self.pl_utils._check_self_comment_rating(
                    comment_id, user_id)
                if check_result:
                    return check_result

            # 更新comment表中的点赞数
            comment_update_query = (
                "UPDATE comment SET num_likes = num_likes + 1 WHERE "
                "comment_id = ?")
            self.pl_utils._execute_db_command(comment_update_query,
                                              (comment_id, ),
                                              commit=True)

            # 在comment_like表中添加记录
            like_insert_query = (
                "INSERT INTO comment_like (comment_id, user_id, created_at) "
                "VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(
                like_insert_query, (comment_id, user_id, current_time),
                commit=True)
            comment_like_id = self.db_cursor.lastrowid  # 获取刚刚插入的点赞记录的ID

            # 记录操作到trace表
            action_info = {
                "comment_id": comment_id,
                "comment_like_id": comment_like_id
            }
            self.pl_utils._record_trace(user_id, ActionType.LIKE_COMMENT.value,
                                        action_info, current_time)
            return {"success": True, "comment_like_id": comment_like_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def unlike_comment(self, agent_id: int, comment_id: int):
        try:
            user_id = agent_id

            # 检查是否已经存在点赞记录
            like_check_query = (
                "SELECT * FROM comment_like WHERE comment_id = ? AND "
                "user_id = ?")
            self.pl_utils._execute_db_command(like_check_query,
                                              (comment_id, user_id))
            result = self.db_cursor.fetchone()

            if not result:
                # 没有存在点赞记录
                return {
                    "success": False,
                    "error": "Comment like record does not exist.",
                }
            # 获取`comment_like_id`
            comment_like_id = result[0]

            # 更新comment表中的点赞数
            comment_update_query = (
                "UPDATE comment SET num_likes = num_likes - 1 WHERE "
                "comment_id = ?")
            self.pl_utils._execute_db_command(
                comment_update_query,
                (comment_id, ),
                commit=True,
            )
            # 在comment_like表中删除记录
            like_delete_query = ("DELETE FROM comment_like WHERE "
                                 "comment_like_id = ?")
            self.pl_utils._execute_db_command(
                like_delete_query,
                (comment_like_id, ),
                commit=True,
            )
            # 记录操作到trace表
            action_info = {
                "comment_id": comment_id,
                "comment_like_id": comment_like_id
            }
            self.pl_utils._record_trace(user_id,
                                        ActionType.UNLIKE_COMMENT.value,
                                        action_info)
            return {"success": True, "comment_like_id": comment_like_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def dislike_comment(self, agent_id: int, comment_id: int):
        if self.recsys_type == RecsysType.REDDIT:
            current_time = self.sandbox_clock.time_transfer(
                datetime.now(), self.start_time)
        else:
            current_time = os.environ["SANDBOX_TIME"]
        try:
            user_id = agent_id

            # 检查是否已经存在不喜欢记录
            dislike_check_query = (
                "SELECT * FROM comment_dislike WHERE comment_id = ? AND "
                "user_id = ?")
            self.pl_utils._execute_db_command(dislike_check_query,
                                              (comment_id, user_id))
            if self.db_cursor.fetchone():
                # 已存在不喜欢记录
                return {
                    "success": False,
                    "error": "Comment dislike record already exists.",
                }

            # 检查要点踩的评论是否是自己发布的
            if self.allow_self_rating is False:
                check_result = self.pl_utils._check_self_comment_rating(
                    comment_id, user_id)
                if check_result:
                    return check_result

            # 更新comment表中的不喜欢数
            comment_update_query = (
                "UPDATE comment SET num_dislikes = num_dislikes + 1 WHERE "
                "comment_id = ?")
            self.pl_utils._execute_db_command(comment_update_query,
                                              (comment_id, ),
                                              commit=True)

            # 在comment_dislike表中添加记录
            dislike_insert_query = (
                "INSERT INTO comment_dislike (comment_id, user_id, "
                "created_at) VALUES (?, ?, ?)")
            self.pl_utils._execute_db_command(
                dislike_insert_query, (comment_id, user_id, current_time),
                commit=True)
            comment_dislike_id = (self.db_cursor.lastrowid)  # 获取刚刚插入的不喜欢记录的ID

            # 记录操作到trace表
            action_info = {
                "comment_id": comment_id,
                "comment_dislike_id": comment_dislike_id,
            }
            self.pl_utils._record_trace(user_id,
                                        ActionType.DISLIKE_COMMENT.value,
                                        action_info, current_time)
            return {"success": True, "comment_dislike_id": comment_dislike_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def undo_dislike_comment(self, agent_id: int, comment_id: int):
        try:
            user_id = agent_id

            # 检查是否已经存在不喜欢记录
            dislike_check_query = (
                "SELECT comment_dislike_id FROM comment_dislike WHERE "
                "comment_id = ? AND user_id = ?")
            self.pl_utils._execute_db_command(dislike_check_query,
                                              (comment_id, user_id))
            dislike_record = self.db_cursor.fetchone()
            if not dislike_record:
                # 不存在不喜欢记录
                return {
                    "success": False,
                    "error": "Comment dislike record does not exist.",
                }
            comment_dislike_id = dislike_record[0]

            # 从comment_dislike表中删除记录
            dislike_delete_query = (
                "DELETE FROM comment_dislike WHERE comment_id = ? AND "
                "user_id = ?")
            self.pl_utils._execute_db_command(dislike_delete_query,
                                              (comment_id, user_id),
                                              commit=True)

            # 更新comment表中的不喜欢数
            comment_update_query = (
                "UPDATE comment SET num_dislikes = num_dislikes - 1 WHERE "
                "comment_id = ?")
            self.pl_utils._execute_db_command(comment_update_query,
                                              (comment_id, ),
                                              commit=True)

            # 记录操作到trace表
            action_info = {
                "comment_id": comment_id,
                "comment_dislike_id": comment_dislike_id,
            }
            self.pl_utils._record_trace(user_id,
                                        ActionType.UNDO_DISLIKE_COMMENT.value,
                                        action_info)
            return {"success": True, "comment_dislike_id": comment_dislike_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def do_nothing(self, agent_id: int):
        try:
            user_id = agent_id

            # 记录操作到trace表
            action_info = {}
            self.pl_utils._record_trace(user_id, ActionType.DO_NOTHING.value,
                                        action_info)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}