from clam_ptb.local_settings import LocalSettings

USER_ANTON = "anton"
USER_MAMONT = "mila"
USER_POLIA = "polia"
ROOT_USER_ID: int = 96009555


LOCAL_SETTINGS = LocalSettings(
    USER_ID_MAP={
        ROOT_USER_ID: USER_ANTON,
        192932807: USER_MAMONT,
        256576988: USER_POLIA,
    },
    ROOT_USER_ID=ROOT_USER_ID,
    BOT_REAL_USER_ID=5532989047,
    BOT_FULL_NAME="@AntonNotifyBot",
    FULL_LOG_CHAT_ID=-4661319516,
)
