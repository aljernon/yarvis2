from yarvis_ptb.local_settings import LocalSettings

USER_ANTON = "anton"
ROOT_USER_ID: int = 96009555


LOCAL_SETTINGS = LocalSettings(
    USER_ID_MAP={
        ROOT_USER_ID: USER_ANTON,
    },
    ROOT_USER_ID=ROOT_USER_ID,
    BOT_REAL_USER_ID=8728802297,
    BOT_FULL_NAME="@ya42352_bot",
    FULL_LOG_CHAT_ID=-4661319516,
)
