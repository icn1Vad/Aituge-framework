"""Official MinerU HTTP app with a bounded batch for the deployment GPU."""
from mineru.cli.fast_api import app
app.state.config["batch_size"] = 1
