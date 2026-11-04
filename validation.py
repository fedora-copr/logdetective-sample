#!/usr/bin/env python3

"""
Script that evaluates accuracy of Log Detective on a few samples of tricky failed build logs.
Uses LLM as a judge to evaluate the accuracy of the responses
in comparison to issue description in sample_metadata.yaml.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from typing import Literal
from urllib.parse import urljoin

from pydantic import BaseModel, Field, ValidationError
import requests
import yaml


def get_api_key_from_file(path: str):
    """Attempt to read API key from a file.
    This is safer than typing it in CLI."""

    with open(path, encoding="utf-8") as key_file:
        return key_file.read().strip()


SYSTEM_ONE_PORT = 9931
JUDGE_STARTUP_TIMEOUT = 3600  # This can include the first-time download
CONFIDENCE_BAND = 0.05  # interval < 0.5 +- CB > is considered low confidence
CONTEXT_SIZE = 8192
UBATCH_SIZE = 1024  # 512 default was not enough for some requests


class SystemOneNoulAnswer(BaseModel):
    """Validate one System One answer for a noul question."""

    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class SystemOneUsage(BaseModel):
    """Token counts reported by System One."""

    input_tokens: int
    output_tokens: int


class SystemOneResponse(BaseModel):
    """System One response specialized for this script's noul question."""

    model: str
    answers: dict[str, SystemOneNoulAnswer] = Field(min_length=1)
    usage: SystemOneUsage


JUDGE_SYSTEM_PROMPT = """\
Compare how well the 'actual_output' addresses the issue described in 'expected_output'.
The comparison is one-directional: does the actual output identify the same root cause?
Ask: "Could a maintainer fix the issue just as well following the actual output as the expected output?"
Do not penalize for: different terminology, higher level of technical detail, different lengths.
"""

JUDGE_CRITERIA = {
    "true": (
        "The actual output identifies the same root cause as the expected output and "
        "provides enough useful detail for a maintainer to act, even if wording or "
        "technical detail differs."
    ),
    "false": (
        "The actual output identifies a different or conflicting root cause, describes "
        "only the same broad area or symptom, or omits the core issue needed to identify "
        "the cause."
    ),
}


def get_noul_probability(expected_text: str, actual_text: str, timeout: float
) -> tuple[float, float]:
    """
    Ask the System One endpoint about semantic similarity of expected_text and actual_text.
    Return response time and probability value (for the noul-type question).

    Args:
        expected_text (str): The expected response text.
        actual_text (str): The actual response text from the API.
        timeout (str): Max timeout for judge request.
    Returns:
        tuple[float, float]: response time in seconds, probability value between 0 and 1
    Raises:
        `requests.exceptions.HTTPError`: System One API request failed
        `ValueError`: Returned response has an unexpected structure.
    """

    topic = "semantic_similarity"
    start_time = time.monotonic()
    request_data = {
        "state": {
            "expected_output": expected_text,
            "actual_output": actual_text,
        },
        "questions": {
            topic: {
                "type": "noul",
                "instructions": JUDGE_SYSTEM_PROMPT,
                "criteria": JUDGE_CRITERIA,
            }
        },
    }
    response = requests.post(
        f"http://localhost:{SYSTEM_ONE_PORT}/v1/systemone",
        json=request_data,
        timeout=timeout,
    )
    response.raise_for_status()

    try:
        response_data = SystemOneResponse.model_validate(response.json())
        probability = response_data.answers[topic].noul
    except (KeyError, TypeError, ValidationError) as e:
        raise ValueError("System One returned an invalid response") from e

    return time.monotonic() - start_time, probability


def traverse_metadata_yamls(directory: str) -> Generator[str]:
    """Generate recursively all paths to sample config YAMLs in a directory."""
    for root, _, files in os.walk(directory):
        for file in files:
            if file == "sample_metadata.yaml":
                yaml_path = os.path.join(root, file)
                yield yaml_path


def create_payload_from_yaml(log_files: list, yaml_path: str) -> dict:
    """
    From the 'log_files' field in sample_metadata.yaml, create the payload
    to be sent to Log Detective server.

    Args:
        log_files (list): List of log file names making the sample.
        yaml_path (str): Path to yaml file, logs are expected to be in the same dir.

    Raises:
        ValueError: Some issue with reading log file read.
    """

    file_list = []
    for log_name in log_files:
        log_file_path = Path(yaml_path).with_name(log_name)
        with open(log_file_path, encoding="utf-8") as f:
            log_file_content = f.read()
        if not log_file_content:
            raise ValueError(f"Empty or invalid log file {log_name}")

        file_list.append({"name": log_name, "content": log_file_content})

    return {"files": file_list}


def get_explanation(
    url: str, data: dict, headers: dict, timeout: float
) -> tuple[float, str]:
    """
    Poll log detective's (async) API for the explanation.
    Return response time and explanation.
    """
    start_time = time.time()
    deadline = time.monotonic() + timeout

    api_response = requests.post(
        url,
        json=data,
        timeout=max(deadline - time.monotonic(), 0),
        headers=headers,
    )
    api_response.raise_for_status()

    task_url = urljoin(url, api_response.headers["Location"])
    task_data = api_response.json()

    while task_data["status"] in {"scheduled", "in_progress", "cancelling"}:
        retry_after = float(api_response.headers.get("Retry-After", 5))
        sleep_time = min(retry_after, deadline - time.monotonic())
        if sleep_time <= 0:
            raise requests.exceptions.Timeout(
                "Timed out waiting for Log Detective analysis"
            )
        time.sleep(sleep_time)
        request_timeout = deadline - time.monotonic()
        if request_timeout <= 0:
            raise requests.exceptions.Timeout(
                "Timed out waiting for Log Detective analysis"
            )
        api_response = requests.get(
            task_url,
            timeout=request_timeout,
            headers=headers,
        )
        api_response.raise_for_status()
        task_data = api_response.json()

    if task_data["status"] != "done":
        raise ValueError(
            f"Log Detective analysis status: {task_data['status']}, task_data: {task_data}"
        )

    actual_response_data = task_data["result"]
    time_elapsed = time.time() - start_time
    actual_issue: str = actual_response_data["explanation"]

    return time_elapsed, actual_issue


def stop_judge_server(process: subprocess.Popen) -> None:
    """Stop the llama process started by this script."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_judge_server(model: str, ubatch_size: int) -> subprocess.Popen:
    """Launch llama, which downloads a Hugging Face GGUF model as needed."""
    llama_binary = shutil.which("llama")
    if llama_binary is None:
        raise RuntimeError("Could not find llama executable.")

    try:
        with socket.create_connection(("localhost", SYSTEM_ONE_PORT), timeout=1):
            raise RuntimeError(
                f"Port {SYSTEM_ONE_PORT} is already in use; stop the process using it."
            )
    except OSError:
        pass

    command = [
        llama_binary, "serve",
        "--hf-repo", model,
        "--host", "localhost",
        "--port", f"{SYSTEM_ONE_PORT}",
        "--ctx-size", f"{CONTEXT_SIZE}",
        "--ubatch-size", f"{ubatch_size}",
        "--flash-attn", "on",
        "--jinja",
        "--cache-ram", "0",
    ]
    print(f"Starting local judge: {' '.join(command)}", flush=True)
    process = subprocess.Popen(command)
    health_url = f"http://localhost:{SYSTEM_ONE_PORT}/health"
    deadline = time.monotonic() + JUDGE_STARTUP_TIMEOUT

    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"llama serve exited during startup (status {process.returncode})"
                )
            try:
                response = requests.get(health_url, timeout=2)
                response.raise_for_status()
                print("Local judge is ready", flush=True)
                return process
            except requests.RequestException:
                time.sleep(1)

        raise TimeoutError(
            f"llama did not become ready within {JUDGE_STARTUP_TIMEOUT}s"
        )
    except BaseException:
        stop_judge_server(process)
        raise


def print_final_stats(
    probabilities: list[float],
    response_times: list[float],
    judge_times: list[float],
) -> None:
    """Display final validation results."""
    passing = sum(int(p >= 0.5) for p in probabilities)
    samples = len(probabilities)
    confidences = [abs(p - 0.5) for p in probabilities]
    lows = sum(int(c <= CONFIDENCE_BAND) for c in confidences)
    normals = sum(
        int(CONFIDENCE_BAND < c < 0.5 - CONFIDENCE_BAND) for c in confidences
    )
    highs = sum(int(c >= 0.5 - CONFIDENCE_BAND) for c in confidences)

    print("\n--- Final Report ".ljust(80, "-"), flush=True)
    print(
        f"\nPassing samples: {passing}/{samples}, "
        f"Confidence stats: {lows} low, {normals} normal, {highs} high"
    )
    print(
        f"Median Probability: {(median(probabilities) * 100):.2f} %, "
        f"Median Log Detective time: {median(response_times):.3f}s, "
        f"Median Judge time: {median(judge_times):.3f}s."
    )


def evaluate_samples(
    directory: str,
    server_address: str,
    log_detective_api_timeout: int,
    log_detective_api_key: str,
    judge_model: str,
    judge_timeout: float,
    ubatch_size: int,
) -> None:
    """
    Traverses a directory to find and evaluate log analysis samples.

    Args:
        directory (str): The path to the directory containing the samples.
        server_address (str): The base address of the server.
    """
    api_endpoint = "/analyze"

    full_api_url = f"{server_address}{api_endpoint}"

    log_detective_request_headers = {}
    if log_detective_api_key:
        log_detective_request_headers["Authorization"] = (
            f"Bearer {log_detective_api_key}"
        )

    print(f"Processing samples' YAML metadata in {directory}...")

    samples = []
    for yaml_path in traverse_metadata_yamls(directory):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                metadata: dict = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Could not parse {yaml_path}: {e}") from e

        if not isinstance(metadata, dict):
            raise TypeError(f"Unexpected YAML structure of {yaml_path}")

        expected_issue = metadata.get("issue")
        log_files = metadata.get("log_files")
        sample_uuid = yaml_path.split("/")[-2]

        if not expected_issue or not log_files:
            raise ValueError(
                f"Invalid {yaml_path}: missing 'issue' or 'log_files' field."
            )

        payload = create_payload_from_yaml(log_files, yaml_path)

        samples.append(
            {
                "expected_issue": expected_issue,
                "log_files": log_files,
                "payload": payload,
                "sample_uuid": sample_uuid,
                "yaml_path": yaml_path,
            }
        )

    if not samples:
        raise ValueError("No samples found.")

    judge_process = start_judge_server(model=judge_model, ubatch_size=ubatch_size)

    try:
        stats = process_samples_with_judge(
            samples=samples,
            full_api_url=full_api_url,
            log_detective_request_headers=log_detective_request_headers,
            log_detective_api_timeout=log_detective_api_timeout,
            judge_timeout=judge_timeout,
        )
    finally:
        stop_judge_server(judge_process)
    print_final_stats(*stats)


def process_samples_with_judge(
    samples: list[dict],
    full_api_url: str,
    log_detective_request_headers: dict,
    log_detective_api_timeout: int,
    judge_timeout: float,
) -> tuple[list[float], list[float], list[float]]:
    print(f"Calling Log Detective API: {full_api_url} for {len(samples)} samples...")

    analysis_results: dict[str, tuple[float, str]] = {}
    max_workers = min(32, len(samples))
    probabilities = []
    elapsed_times = []
    judge_times = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_sample = {}
        for sample in samples:
            sample_uuid = sample["sample_uuid"]
            future_to_sample[
                executor.submit(
                    get_explanation,
                    url=full_api_url,
                    data=sample["payload"],
                    headers=log_detective_request_headers,
                    timeout=log_detective_api_timeout,
                )
            ] = sample

        for future in as_completed(future_to_sample):
            sample = future_to_sample[future]
            sample_uuid = sample["sample_uuid"]

            try:
                analysis_results[sample_uuid] = future.result()
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError,
            ) as e:
                raise ConnectionError(
                    f"Could not obtain Log Detective response for sample {sample_uuid}: {e}"
                ) from e
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"Could not process the Log Detective response for {sample_uuid}"
                ) from e

    for idx, sample in enumerate(samples, start=1):
        expected_issue: str = sample["expected_issue"]
        sample_uuid: str = sample["sample_uuid"]
        logs: list[str] = sample["log_files"]
        uuid_prefix = sample_uuid.split("-")[0]

        time_elapsed, actual_issue = analysis_results[sample_uuid]
        elapsed_times.append(time_elapsed)

        print(
            f"\n--- ({idx}) Analyzing {uuid_prefix} : {' '.join(logs)} ".ljust(80, "-")
        )
        print("\n[Expected Response]")
        print(expected_issue.strip())
        print("\n[Actual Response]")
        print(actual_issue.strip())

        try:
            judge_time, probability = get_noul_probability(
                expected_issue, actual_issue, judge_timeout,
            )
        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"Failed to parse noul probability score for {sample_uuid}: {e}"
            ) from e
        judge_times.append(judge_time)
        probabilities.append(probability)

        print(
            f"\n[Judge] Similarity: {(probability * 100):.1f} %, "
            f"Response time: {time_elapsed:.3f}s, "
            f"Judge time: {judge_time:.3f}s"
        )

    return probabilities, elapsed_times, judge_times


def main():
    """
    Main function to parse arguments and run the evaluation script.
    """
    parser = argparse.ArgumentParser(
        description="Evaluate AI system performance by comparing expected and actual responses.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--data-directory",
        help="Path to the directory containing the sample data.",
        default="./data",
    )
    parser.add_argument(
        "--judge-model",
        default="waseigo/elephant-1-decision-GGUF:Q4_K_M",
        help=(
            "Hugging Face GGUF model repository and optional quantization, "
            "for example org/model:quantization. Used when starting llama."
        ),
    )
    parser.add_argument(
        "--judge-timeout",
        type=float,
        default=60,  # elephant usually takes 20-30 seconds
        help="Request timeout for System One API (judge)"
    )
    parser.add_argument(
        "--log-detective-url",
        help="Base URL of the Log Detective server (e.g. http://localhost:8080).",
        required=True,
    )
    parser.add_argument(
        "--log-detective-api-timeout",
        help="Request timeout for Log Detective API",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--log-detective-api-key",
        help="Path to file with Log Detective API key, if one is necessary",
        type=str,
        default="",
    )
    parser.add_argument(
        "--ubatch-size",
        help="Controls the physical batch size (in tokens) for prompt processing.",
        type=int,
        default=UBATCH_SIZE,
    )
    args = parser.parse_args()

    if not os.path.isdir(args.data_directory):
        print(f"Error: Directory not found at '{args.data_directory}'", file=sys.stderr)
        sys.exit(1)

    log_detective_api_key = ""
    if args.log_detective_api_key:
        log_detective_api_key = get_api_key_from_file(args.log_detective_api_key)

    evaluate_samples(
        directory=args.data_directory,
        server_address=args.log_detective_url,
        log_detective_api_timeout=args.log_detective_api_timeout,
        log_detective_api_key=log_detective_api_key,
        judge_model=args.judge_model,
        judge_timeout=args.judge_timeout,
        ubatch_size=args.ubatch_size
    )


if __name__ == "__main__":
    main()
