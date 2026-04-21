import os
import sys

from dotenv import load_dotenv
from flask import Flask, render_template

# Load .env from project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

# Add project root (for `dashboard.*` imports) and yarvis_ptb to path
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "yarvis_ptb"))

os.environ.setdefault("SETTINGS_NAME", "anton")

app = Flask(__name__)


# ── HTML Routes ──────────────────────────────────────────────────────────────


@app.route("/")
@app.route("/messages")
def messages_page():
    return render_template("messages.html")


@app.route("/schedules")
def schedules_page():
    return render_template("schedules.html")


@app.route("/agent")
def agent_page():
    return render_template("agent.html")


@app.route("/agents")
def agents_page():
    return render_template("agents.html")


@app.route("/workspace")
def workspace_page():
    return render_template("workspace.html")


@app.route("/locations")
def locations_page():
    return render_template("locations.html")


# ── Register Blueprints ─────────────────────────────────────────────────────

from dashboard.routes.agent_view import bp as agent_view_bp
from dashboard.routes.agents import bp as agents_bp
from dashboard.routes.chat import bp as chat_bp
from dashboard.routes.locations import bp as locations_bp
from dashboard.routes.messages import bp as messages_bp
from dashboard.routes.schedules import bp as schedules_bp
from dashboard.routes.workspace import bp as workspace_bp

app.register_blueprint(messages_bp)
app.register_blueprint(agents_bp)
app.register_blueprint(agent_view_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(locations_bp)
app.register_blueprint(schedules_bp)
app.register_blueprint(workspace_bp)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
