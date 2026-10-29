"""系统开关（全局只读开关等），运营后台可改。"""

from sqlalchemy.orm import Session

from .models import SystemFlag


def get_flag(db: Session, key: str) -> dict:
    flag = db.get(SystemFlag, key)
    return flag.value if flag else {}


def set_flag(db: Session, key: str, value: dict) -> None:
    flag = db.get(SystemFlag, key)
    if flag:
        flag.value = value
    else:
        db.add(SystemFlag(key=key, value=value))


def kill_switch_on(db: Session) -> bool:
    return bool(get_flag(db, "kill_switch").get("enabled"))
