"""Create the first admin account and the L2 roster from settings.json."""
import sys
from sqlalchemy import select
from auth import hash_password
from models import QueueMembership, User
from store import init_db, load_settings, session_scope

DEFAULT_PW = "changeme"


def run(admin_user="admin", admin_name="Administrator"):
    init_db()
    s_cfg = load_settings()
    created = []
    with session_scope() as s:
        if not s.scalar(select(User).where(User.username == admin_user)):
            s.add(User(username=admin_user, display_name=admin_name,
                       password_hash=hash_password(DEFAULT_PW), role="admin"))
            created.append(f"{admin_name} (admin)")
        s.flush()
        for queue, names in s_cfg["roster"].items():
            for name in names:
                u = s.scalar(select(User).where(User.display_name == name))
                if not u:
                    uname = name.lower().replace(" ", ".")
                    u = User(username=uname, display_name=name,
                             password_hash=hash_password(DEFAULT_PW), role="l2")
                    s.add(u)
                    s.flush()
                    created.append(f"{name} ({uname})")
                if not s.scalar(select(QueueMembership).where(
                        QueueMembership.user_id == u.id, QueueMembership.queue == queue)):
                    s.add(QueueMembership(user_id=u.id, queue=queue))
    print(f"Created {len(created)} account(s):")
    for c in created:
        print("  ", c)
    print(f"\nEveryone starts with the password '{DEFAULT_PW}' and must change it on first sign-in.")


if __name__ == "__main__":
    run(*(sys.argv[1:3] or []))
