from __future__ import annotations

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import scheduler_v1
from google.protobuf import field_mask_pb2

from config import Config

MAX_JOBS = 3


class SchedulerError(Exception):
    pass


class SchedulerAdmin:
    def __init__(self, cfg: Config):
        if not cfg.gcp_project_id or not cfg.scheduler_service_account or not cfg.main_function_url:
            raise SchedulerError("Не настроены GCP_PROJECT_ID / SCHEDULER_SERVICE_ACCOUNT / MAIN_FUNCTION_URL")
        self._client = scheduler_v1.CloudSchedulerClient()
        self._parent = f"projects/{cfg.gcp_project_id}/locations/{cfg.gcp_region}"
        self._cfg = cfg

    def _full_name(self, name: str) -> str:
        return f"{self._parent}/jobs/{name}"

    def list_jobs(self) -> list[scheduler_v1.Job]:
        return list(self._client.list_jobs(parent=self._parent))

    def get_job(self, name: str) -> scheduler_v1.Job | None:
        try:
            return self._client.get_job(name=self._full_name(name))
        except GoogleAPICallError:
            return None

    def create_job(self, name: str, cron: str, timezone: str) -> scheduler_v1.Job:
        jobs = self.list_jobs()
        if len(jobs) >= MAX_JOBS:
            raise SchedulerError(f"Достигнут лимит {MAX_JOBS} графиков. Удалите один, чтобы добавить новый.")
        if self.get_job(name) is not None:
            raise SchedulerError(f"График с именем «{name}» уже существует.")

        job = scheduler_v1.Job(
            name=self._full_name(name),
            schedule=cron,
            time_zone=timezone,
            http_target=scheduler_v1.HttpTarget(
                uri=self._cfg.main_function_url,
                http_method=scheduler_v1.HttpMethod.POST,
                oidc_token=scheduler_v1.OidcToken(
                    service_account_email=self._cfg.scheduler_service_account,
                ),
            ),
        )
        try:
            return self._client.create_job(parent=self._parent, job=job)
        except GoogleAPICallError as exc:
            raise SchedulerError(f"Не удалось создать график: {exc.message}") from exc

    def update_schedule(self, name: str, cron: str, timezone: str | None = None) -> scheduler_v1.Job:
        if self.get_job(name) is None:
            raise SchedulerError(f"График «{name}» не найден.")
        job = scheduler_v1.Job(name=self._full_name(name), schedule=cron)
        mask_paths = ["schedule"]
        if timezone:
            job.time_zone = timezone
            mask_paths.append("time_zone")
        try:
            return self._client.update_job(
                job=job, update_mask=field_mask_pb2.FieldMask(paths=mask_paths)
            )
        except GoogleAPICallError as exc:
            raise SchedulerError(f"Не удалось обновить график: {exc.message}") from exc

    def delete_job(self, name: str) -> None:
        try:
            self._client.delete_job(name=self._full_name(name))
        except GoogleAPICallError as exc:
            raise SchedulerError(f"Не удалось удалить график: {exc.message}") from exc

    def pause_job(self, name: str) -> scheduler_v1.Job:
        return self._client.pause_job(name=self._full_name(name))

    def resume_job(self, name: str) -> scheduler_v1.Job:
        return self._client.resume_job(name=self._full_name(name))

    def run_job(self, name: str) -> scheduler_v1.Job:
        return self._client.run_job(name=self._full_name(name))
