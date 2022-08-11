from collections import defaultdict
from time import sleep, time
from threading import Lock
from dataclasses import dataclass
from typing import TYPE_CHECKING
from ding.framework import task, EventEnum
from ditk import logging
from ding.utils import DistributedWriter

from ding.utils.sparse_logging import log_every_sec

if TYPE_CHECKING:
    from easydict import EasyDict
    from ding.framework import Task, Context
    from ding.league.v2 import BaseLeague
    from ding.league.player import PlayerMeta
    from ding.league.v2.base_league import Job


class LeagueCoordinator:

    def __init__(self, cfg: "EasyDict", league: "BaseLeague") -> None:
        self.league = league
        self._lock = Lock()
        self._total_send_jobs = 0
        self._total_recv_jobs = 0
        self._eval_frequency = 10
        self._running_jobs = dict()
        self._last_collect_time = None
        self._total_collect_time = None
        self._writer = DistributedWriter.get_instance()

        self._traj_num = 0
        self._last_get_data_time = None
        self._total_get_data_time = 0
        self._pre_collect_finished = False

        task.on(EventEnum.ACTOR_GREETING, self._on_actor_greeting)
        task.on(EventEnum.LEARNER_SEND_META, self._on_learner_meta)
        task.on(EventEnum.ACTOR_FINISH_JOB, self._on_actor_job)
        task.on(EventEnum.ACTOR_SEND_DATA.format(player='main_player_default_0'), self._count_data)

    def _count_data(self, data):
        if self._last_get_data_time is None:
            self._last_get_data_time = time()

        if self._total_get_data_time >= 60 * 10 and self._pre_collect_finished is False:
            self._pre_collect_finished = True
            self._total_get_data_time = 0

        finish_get_data_time = time()
        current_get_data_time = finish_get_data_time - self._last_get_data_time
        self._total_get_data_time += current_get_data_time
        self._last_get_data_time = finish_get_data_time

        if self._pre_collect_finished:
            current_traj_num = 0
            for env_trajectories in data.train_data:
                for traj in env_trajectories.trajectories:
                    self._traj_num += 1
                    current_traj_num += 1
            logging.info(
                "[Coordinator {}] recieve {} traj, current send speed is {} traj/s, total send speed is {} traj/s".format(
                    task.router.node_id, current_traj_num, current_traj_num / current_get_data_time,
                    self._traj_num / self._total_get_data_time
                )
            )
            self._writer.add_scalar(
                "current_traj_speed-total_recv_trajs", current_traj_num / current_get_data_time, self._total_get_data_time
            )
            self._writer.add_scalar(
                "total_traj_speed-total_recv_trajs", self._traj_num / self._total_get_data_time, self._total_get_data_time
            )

    def _on_actor_greeting(self, actor_id):
        logging.info("[Coordinator {}] recieve actor {} greeting".format(task.router.node_id, actor_id))
        if self._last_collect_time is None:
            self._last_collect_time = time()
        if self._total_collect_time is None:
            self._total_collect_time = 0
        with self._lock:
            player_num = len(self.league.active_players_ids)
            player_id = self.league.active_players_ids[self._total_send_jobs % player_num]
            job = self.league.get_job_info(player_id)
            job.job_no = self._total_send_jobs
            self._total_send_jobs += 1
        if job.job_no > 0 and job.job_no % self._eval_frequency == 0:
            job.is_eval = True
        job.actor_id = actor_id
        self._running_jobs["actor_{}".format(actor_id)] = job
        task.emit(EventEnum.COORDINATOR_DISPATCH_ACTOR_JOB.format(actor_id=actor_id), job)

    def _on_learner_meta(self, player_meta: "PlayerMeta"):
        log_every_sec(
            logging.INFO, 5,
            '[Coordinator {}] recieve learner meta from player {}'.format(task.router.node_id, player_meta.player_id)
        )
        self.league.update_active_player(player_meta)
        self.league.create_historical_player(player_meta)

    def _on_actor_job(self, job: "Job"):
        self._total_recv_jobs += 1
        old_time = self._last_collect_time
        self._last_collect_time = time()
        self._total_collect_time += self._last_collect_time - old_time
        logging.info(
            "[Coordinator {}] recieve actor finished job, player {}, recieve {} jobs in total, collect job speed is {} s/job"
            .format(
                task.router.node_id, job.launch_player, self._total_recv_jobs,
                self._total_collect_time / self._total_recv_jobs
            )
        )
        self._writer.add_scalar(
            "coordinator_collect_job_speed-total_recv_jobs", self._total_collect_time / self._total_recv_jobs,
            self._total_recv_jobs
        )
        self.league.update_payoff(job)

    def __del__(self):
        logging.info("[Coordinator {}] all tasks finished, coordinator closed".format(task.router.node_id))

    def __call__(self, ctx: "Context") -> None:
        sleep(1)
        log_every_sec(
            logging.INFO, 600, "[Coordinator {}] running jobs {}".format(task.router.node_id, self._running_jobs)
        )
