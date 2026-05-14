import logging
import json

def show_results(output: str, res: dict) -> None:
    logging.info(json.dumps(res, indent=4))
    with open(output, "w") as f:
        json.dump(res, f, indent=4)
        logging.info(f"Results saved to {output}")