"""认证服务：注册 / 登录（含锁定策略）/ 登出 / 审计落库。

对应：PRD F6.1（注册）、F6.2（登录 JWT / 连续 5 次失败锁定 5 分钟 / 退出）、S4（鉴权与隔离）、
      UIUX 9.2（错误码呈现）。

**事务陷阱（实现要点）**：登录失败路径需要「先把失败计数与审计落库，再抛业务异常」。
本模块统一使用 `AsyncSession`，在抛错**之前**显式 `await session.commit()`，
否则异常会让会话回滚、失败计数丢失（锁定策略形同虚设）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from app.agent import checkpointer
from app.core.errors import (
    LOCK_MINUTES,
    MAX_LOGIN_FAILURES,
    BizError,
    account_disabled,
    account_locked,
    credentials_invalid,
    not_found,
    username_taken,
    validation_error,
)
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import (
    Analysis,
    AuditLog,
    Conversation,
    EvalRun,
    LLMCall,
    Message,
    UploadedReport,
    User,
)
from app.db.session import control_session
from app.schemas.auth import (
    ClearRecordsIn,
    ClearRecordsOut,
    DeleteAccountIn,
    DeleteAccountOut,
    LoginIn,
    PasswordChangeIn,
    ProfileUpdateIn,
    RegisterIn,
    TokenOut,
    UserOut,
)

logger = logging.getLogger("zdxb.auth")

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    """与库内 `datetime('now','localtime')` 一致的时间文本（可直接字符串比较）。"""
    return datetime.now().strftime(TIME_FMT)


def normalize_username(username: str) -> str:
    """用户名不区分大小写：统一转小写后存储与比对（决策）。"""
    return username.strip().lower()


def _audit(
    event_type: str,
    severity: str,
    action: str,
    *,
    user_id: int | None = None,
    rule_id: str | None = None,
    payload: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """构造审计行。`payload` 严禁包含明文密码或 token（PRD S5）。"""
    return AuditLog(
        user_id=user_id,
        event_type=event_type,
        severity=severity,
        rule_id=rule_id,
        action=action,
        payload=payload,
        ip=ip,
        user_agent=user_agent,
        created_at=_now(),
    )


async def get_user_by_id(user_id: int) -> User | None:
    async with control_session() as session:
        return await session.scalar(select(User).where(User.id == user_id))


async def register(
    payload: RegisterIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenOut:
    """注册并**自动登录**（决策：注册成功即返回 token）。"""
    username = normalize_username(payload.username)
    timestamp = _now()

    async with control_session() as session:
        duplicated = await session.scalar(select(User.id).where(User.username == username))
        if duplicated is not None:
            raise username_taken()

        user = User(
            username=username,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name or username,
            email=payload.email,
            is_active=1,
            login_fail_count=0,
            last_login_at=timestamp,  # 注册即登录，故写入最近登录时间
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(user)
        await session.flush()  # 取得自增主键
        session.add(
            _audit(
                "auth_register",
                "low",
                "allowed",
                user_id=user.id,
                payload=f"username={username}",
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        user_id, user_username = user.id, user.username

    token, expires_at = create_access_token(user_id, user_username)
    return TokenOut(
        token=token, expires_at=expires_at.isoformat(), user=UserOut.model_validate(user)
    )


async def login(
    payload: LoginIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TokenOut:
    """登录。严格策略：5 次失败锁 5 分钟；锁定期内直接 429；成功登录才清零计数。"""
    username = normalize_username(payload.username)
    now_dt = datetime.now()
    timestamp = now_dt.strftime(TIME_FMT)

    async with control_session() as session:
        user = await session.scalar(select(User).where(User.username == username))

        # ① 锁定期内：不校验密码，直接 429
        if user is not None and user.locked_until and user.locked_until > timestamp:
            remain = int(
                (datetime.strptime(user.locked_until, TIME_FMT) - now_dt).total_seconds()
            )
            session.add(
                _audit(
                    "auth_locked",
                    "high",
                    "blocked",
                    user_id=user.id,
                    rule_id="lock_window",
                    payload=f"locked_until={user.locked_until}",
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()  # 先落审计，再抛错
            raise account_locked(max(remain, 1))

        # ② 凭据错误（含“用户名不存在”，文案统一，不泄露账号是否存在）
        if user is None or not verify_password(payload.password, user.password_hash):
            if user is None:
                session.add(
                    _audit(
                        "auth_login_failed",
                        "medium",
                        "warned",
                        rule_id="bad_credentials",
                        payload=f"username={username}",
                        ip=ip,
                        user_agent=user_agent,
                    )
                )
                await session.commit()
                raise credentials_invalid()

            user.login_fail_count += 1
            user.updated_at = timestamp
            locked = user.login_fail_count >= MAX_LOGIN_FAILURES
            if locked:
                user.locked_until = (now_dt + timedelta(minutes=LOCK_MINUTES)).strftime(TIME_FMT)
            session.add(
                _audit(
                    "auth_locked" if locked else "auth_login_failed",
                    "high" if locked else "medium",
                    "blocked" if locked else "warned",
                    user_id=user.id,
                    rule_id=(
                        f"login_fail_count>={MAX_LOGIN_FAILURES}"
                        if locked
                        else "bad_credentials"
                    ),
                    payload=f"failures={user.login_fail_count}",
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()  # 计数与审计必须先落库，否则锁定策略失效
            if locked:
                raise account_locked(LOCK_MINUTES * 60)
            raise credentials_invalid()

        # ③ 账号停用
        if not user.is_active:
            session.add(
                _audit(
                    "auth_forbidden",
                    "medium",
                    "blocked",
                    user_id=user.id,
                    rule_id="account_disabled",
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()
            raise account_disabled()

        # ④ 登录成功：清零失败计数、刷新最近登录时间
        user.login_fail_count = 0
        user.locked_until = None
        user.last_login_at = timestamp
        user.updated_at = timestamp
        session.add(
            _audit("auth_login", "low", "allowed", user_id=user.id, ip=ip, user_agent=user_agent)
        )
        await session.commit()

        user_id, user_username = user.id, user.username

    token, expires_at = create_access_token(user_id, user_username)
    return TokenOut(
        token=token, expires_at=expires_at.isoformat(), user=UserOut.model_validate(user)
    )


async def logout(
    user_id: int,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """退出登录：按 PRD F6.2 由前端清除 token；服务端只落一条审计（可追溯）。"""
    async with control_session() as session:
        session.add(
            _audit("auth_logout", "low", "allowed", user_id=user_id, ip=ip, user_agent=user_agent)
        )
        await session.commit()


# ── 个人中心（PRD F6.4）：编辑资料 / 修改密码 / 清空历史 / 删除账号 ──────────


async def update_profile(
    user_id: int,
    payload: ProfileUpdateIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserOut:
    """编辑资料：只改**显示名 / 邮箱**（用户名是账号标识，不可改）。

    **未提交的字段保持原值**：用 `model_fields_set` 区分"没传"与"显式传 null（清空）"，
    否则只想改邮箱的用户会顺手把显示名清空（前端表单只送改动的字段）。
    """
    fields = payload.model_fields_set
    if not fields:
        raise validation_error("至少需要指定 display_name 或 email 之一")

    async with control_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("用户不存在")
        before = {"display_name": user.display_name, "email": user.email}
        if "display_name" in fields:
            user.display_name = payload.display_name
        if "email" in fields:
            user.email = payload.email
        user.updated_at = _now()
        after = {"display_name": user.display_name, "email": user.email}
        session.add(
            _audit(
                "auth_profile_updated",
                "low",
                "allowed",
                user_id=user_id,
                payload=json.dumps({"before": before, "after": after}, ensure_ascii=False),
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
        return UserOut.model_validate(user)


async def change_password(
    user_id: int,
    payload: PasswordChangeIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """修改密码：校验当前密码 → 换新 hash（bcrypt 重新加盐）。

    **说明（如实告知，不假装有服务端会话）**：本项目 JWT 无服务端黑名单，改密后**已签发的 token
    仍然有效**（不会把其它设备踢下线）；前端在成功提示里说明这一点。
    """
    async with control_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("用户不存在")
        if not verify_password(payload.old_password, user.password_hash):
            session.add(
                _audit(
                    "auth_password_change_failed",
                    "medium",
                    "warned",
                    user_id=user_id,
                    rule_id="bad_old_password",
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()  # 先落审计再抛错（与登录失败同一个事务陷阱）
            raise validation_error("当前密码不正确")
        if payload.old_password == payload.new_password:
            raise validation_error("新密码不能与当前密码相同")

        user.password_hash = hash_password(payload.new_password)
        user.updated_at = _now()
        session.add(
            _audit(
                "auth_password_changed",
                "medium",
                "allowed",
                user_id=user_id,
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
    logger.info("用户 %s 修改密码成功", user_id)


async def clear_records(
    user_id: int,
    payload: ClearRecordsIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ClearRecordsOut:
    """清空历史记录（危险操作，前端二次确认后提交）。

    - `records`：只清**分析记录**——`messages.analysis_id` 由外键 `ON DELETE SET NULL` 置空，
      因此**对话文本保留**（与"在分析记录里逐条删除"的既定语义一致）；
    - `all`：连同**会话与对话历史**一起清空（置顶会话也在内），再补删"没有会话的游离记录"。

    `llm_calls` 一律随分析记录 `ON DELETE CASCADE` 清掉（含评测批次产生的那些）。
    """
    async with control_session() as session:
        # 先数后删：回显"清掉了多少"，也让审计里的数字可核对
        llm_calls = (
            await session.scalar(
                select(func.count())
                .select_from(LLMCall)
                .join(Analysis, LLMCall.analysis_id == Analysis.id)
                .where(Analysis.user_id == user_id)
            )
        ) or 0
        records = (
            await session.scalar(
                select(func.count()).select_from(Analysis).where(Analysis.user_id == user_id)
            )
        ) or 0

        conversations = messages = 0
        if payload.scope == "all":
            conversations = (
                await session.scalar(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.user_id == user_id)
                )
            ) or 0
            messages = (
                await session.scalar(
                    select(func.count()).select_from(Message).where(Message.user_id == user_id)
                )
            ) or 0
            # 检查点级联（阶段 3）：会话即将删除，先取出编号，连同图状态一起清掉
            thread_ids = [
                row[0]
                for row in (
                    await session.execute(
                        select(Conversation.conversation_code).where(
                            Conversation.user_id == user_id
                        )
                    )
                ).all()
            ]
            await session.execute(delete(Conversation).where(Conversation.user_id == user_id))
            await checkpointer.delete_threads_in_session(session, thread_ids)

        # 分析记录：两种口径都要删（`all` 时上面的会话级联已删掉大部分，这里兜住"无会话的游离记录"）
        await session.execute(delete(Analysis).where(Analysis.user_id == user_id))

        session.add(
            _audit(
                "profile_records_cleared",
                "medium",
                "allowed",
                user_id=user_id,
                payload=json.dumps(
                    {
                        "scope": payload.scope,
                        "records": records,
                        "conversations": conversations,
                        "messages": messages,
                        "llm_calls": llm_calls,
                    },
                    ensure_ascii=False,
                ),
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()

    logger.info(
        "用户 %s 清空历史记录：scope=%s 记录 %d 会话 %d",
        user_id, payload.scope, records, conversations,
    )
    return ClearRecordsOut(
        scope=payload.scope,
        deleted_records=records,
        deleted_conversations=conversations,
        deleted_messages=messages,
        deleted_llm_calls=llm_calls,
    )


async def delete_account(
    user_id: int,
    payload: DeleteAccountIn,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> DeleteAccountOut:
    """删除账号（不可撤销，危险区最后一格）：**密码 + 用户名双确认**。

    删除链（数据库设计 4.x 的外键已按此意图定义，见 `power_agent.sql` 注释）：

    1. **上传报表**：先走 `report_write_service.delete_report`（受控写锁 + `DROP TABLE` + 删落盘目录），
       预置报表（`is_preset = 1`）不属于任何用户，天然不受影响；
    2. **评测批次**：`eval_runs.user_id` 是 `ON DELETE SET NULL`（不跟着用户走），逐题结果属个人数据，
       因此**显式删除**；其 `llm_calls` 随 `eval_run_id` 级联清掉；
    3. **业务数据行**：删除 `users` 行 → 会话 / 消息 / 分析记录 / 报表元数据由 `ON DELETE CASCADE` 清理；
    4. **安全日志**：`audit_logs.user_id` 是 `ON DELETE SET NULL` → **行保留、归属置空**
       （PRD F6.4「安全日志保留」），账号删除本身也写一条 `auth_account_deleted`（high）。
    """
    async with control_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise not_found("用户不存在")
        if not verify_password(payload.password, user.password_hash):
            session.add(
                _audit(
                    "auth_delete_account_failed",
                    "high",
                    "blocked",
                    user_id=user_id,
                    rule_id="bad_password",
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()
            raise validation_error("密码不正确，账号未删除")
        if normalize_username(payload.confirm_username) != user.username:
            raise validation_error("用户名不一致，账号未删除（请输入完整用户名以确认）")

        username = user.username
        report_codes = list(
            (
                await session.scalars(
                    select(UploadedReport.report_code).where(
                        UploadedReport.user_id == user_id, UploadedReport.is_preset == 0
                    )
                )
            ).all()
        )
        conversations = (
            await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.user_id == user_id)
            )
        ) or 0
        records = (
            await session.scalar(
                select(func.count()).select_from(Analysis).where(Analysis.user_id == user_id)
            )
        ) or 0

    # ① 报表清理（自带进程内写锁与独立会话，**必须放在主事务之外**：长任务 + 独立提交）
    deleted_reports = 0
    for code in report_codes:
        try:
            from app.services import report_write_service  # 延迟导入：避免服务层循环引用

            await report_write_service.delete_report(
                user_id, code, ip=ip, user_agent=user_agent
            )
            deleted_reports += 1
        except BizError as exc:  # 单张报表清理失败不阻断账号删除（否则用户永远删不掉账号）
            logger.warning("删除账号清理报表 %s 失败（继续）：%s", code, exc.message)

    async with control_session() as session:
        # ② 评测批次（显式删，逐题结果属个人数据）
        eval_runs = (
            await session.execute(delete(EvalRun).where(EvalRun.user_id == user_id))
        ).rowcount or 0

        # ③ 审计先落库：随后删用户行时 user_id 被置 NULL，**日志本身保留**（合规留痕）
        session.add(
            _audit(
                "auth_account_deleted",
                "high",
                "allowed",
                user_id=user_id,
                payload=json.dumps(
                    {"username": username, "reports": deleted_reports,
                     "conversations": conversations, "records": records},
                    ensure_ascii=False,
                ),
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.flush()
        kept_logs = (
            await session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.user_id == user_id)
            )
        ) or 0

        # ④ 删用户行 → 级联清理会话 / 消息 / 分析记录 / 报表元数据
        #    检查点（图状态）不在外键体系内，故先取出该用户全部会话编号再显式清理（阶段 3）
        thread_ids = [
            row[0]
            for row in (
                await session.execute(
                    select(Conversation.conversation_code).where(Conversation.user_id == user_id)
                )
            ).all()
        ]
        await session.execute(delete(User).where(User.id == user_id))
        await checkpointer.delete_threads_in_session(session, thread_ids)
        await session.commit()

    logger.warning("账号已删除：%s（用户 %s）", username, user_id)
    return DeleteAccountOut(
        username=username,
        deleted_reports=deleted_reports,
        deleted_conversations=conversations,
        deleted_records=records,
        deleted_eval_runs=int(eval_runs),
        kept_audit_logs=int(kept_logs),
    )
