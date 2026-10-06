"""Local evaluation only: no dotenv discovery, telemetry, or hosted result publishing."""

import os

os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["DEEPEVAL_UPDATE_WARNING_OPT_IN"] = "0"
os.environ["RAGAS_DO_NOT_TRACK"] = "true"
