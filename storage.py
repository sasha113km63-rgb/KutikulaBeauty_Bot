import json
from sqlalchemy import select
from db import SessionLocal, User, DialogState

async def upsert_user(tg_id: int, name: str | None = None):
    async with SessionLocal() as session:
        res = await session.execute(select(User).where(User.tg_id == tg_id))
        user = res.scalar_one_or_none()
        if not user:
            user = User(tg_id=tg_id, name=name)
            session.add(user)
        else:
            if name and not user.name:
                user.name = name
        await session.commit()

async def get_state(tg_id: int) -> tuple[str, dict]:
    async with SessionLocal() as session:
        res = await session.execute(select(DialogState).where(DialogState.tg_id == tg_id))
        st = res.scalar_one_or_none()
        if not st:
            return "idle", {}
        try:
            return st.step, json.loads(st.payload or "{}")
        except Exception:
            return st.step, {}

async def set_state(tg_id: int, step: str, payload: dict):
    import json as _json
    async with SessionLocal() as session:
        res = await session.execute(select(DialogState).where(DialogState.tg_id == tg_id))
        st = res.scalar_one_or_none()
        if not st:
            st = DialogState(tg_id=tg_id, step=step, payload=_json.dumps(payload, ensure_ascii=False))
            session.add(st)
        else:
            st.step = step
            st.payload = _json.dumps(payload, ensure_ascii=False)
        await session.commit()

async def reset_state(tg_id: int):
    await set_state(tg_id, "idle", {})
