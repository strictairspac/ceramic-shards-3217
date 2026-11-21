from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class MoveStep(StrEnum):
    COPY = 'copy_verified'        # 复制校验：目标库先计占用
    SWITCH = 'ownership_switched'  # 归属原子切换
    CLEANUP = 'source_cleaned'    # 源副本清理确认：源库释放空间


class ReceiptOutcome(StrEnum):
    SUCCESS = 'success'
    FAILURE = 'failure'  # 失败后保留一次重试机会；重试仍失败则条目阻塞


class LedgerStatus(StrEnum):
    IN_PROGRESS = 'in_progress'
    COMPLETE = 'complete'
    BLOCKED = 'blocked'
