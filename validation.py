#!/usr/bin/env python3

"""
Script that evaluates accuracy of Log Detective on a few samples of tricky failed build logs.
Uses LLM as a judge to evaluate the accuracy of the responses
in comparison to issue description in sample_metadata.yaml.
"""

import argparse
import os
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from urllib.parse import urljoin

import openai
import requests
import yaml
from pydantic import BaseModel, Field, ValidationError


def get_api_key_from_file(path: str):
    """Attempt to read API key from a file.
    This is safer than typing it in CLI."""

    with open(path, encoding="utf-8") as key_file:
        return key_file.read().strip()


class SimilarityScore(BaseModel):
    """Defines the structure for the similarity score response from the LLM."""

    score: int = Field(
        ..., ge=1, le=10, description="The similarity score from 1 to 10."
    )


JUDGE_SYSTEM_PROMPT = """\
Rate how well the 'actual_output' addresses the issue described in 'expected_output'.
The comparison is one-directional: does the actual output identify the same root cause?
Ask: "Could a maintainer fix the issue just as well following the actual output as the expected output?"

Score on an integer scale from 1 to 10:
- 1: Completely wrong topic or root cause.
- 3-5: Same general area, but missing (or not fully addressing) the core issue.
- 6: Identifies the correct root cause, but lacks enough detail to act on it. The explanation would be enough for an experienced packager, an inexperienced will probably lack information to efficiently fix the issue.
- 7-9: Correctly identifies the root cause; more detail or different terminology is fine.
- 10: Fully and precisely addresses everything in the expected output.

Do not penalize for: different terminology, higher level of technical detail, different lengths.
"""


def get_similarity_score(
    expected_text: str, actual_text: str, llm_client: openai.OpenAI, llm_model: str
) -> int:
    """
    Uses a Large Language Model to score the similarity between two texts.

    Args:
        expected_text (str): The expected response text.
        actual_text (str): The actual response text from the API.
        llm_model (str): The LLM model to use for the evaluation.

    Returns:
        int: A similarity score from 1 to 10.

    Raises:
        `openai.APIError`:
        `openai.APIConnectionError`:
        `ValidationError`:
        `KeyError`:
        `TypeError`:
    """

    judge_user_prompt = "\n".join(
        [
            "expected_output:",
            expected_text,
            "",
            "actual_output:",
            actual_text,
        ]
    )
    response = llm_client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": judge_user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "rated-snippet-analysis",
                "schema": SimilarityScore.model_json_schema(),
            },
        },
    )
    content = response.choices[0].message.content

    if not isinstance(content, str):
        raise TypeError(f"Invalid response from LLM {content}")

    score = SimilarityScore.model_validate_json(content)
    return score.score


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
    """Poll log detective's (async) API for the explanation."""
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


def evaluate_samples(
    directory: str,
    server_address: str,
    llm_url: str,
    llm_model: str,
    llm_token: str,
    log_detective_api_timeout: int,
    log_detective_api_key: str = "",
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

    client = openai.OpenAI(base_url=llm_url, api_key=llm_token)
    scores = []
    elapsed_times = []

    median_score = 0
    median_elapsed_time = 0
    samples_passing = 0

    print(f"Processing samples' YAML metadata in {directory}...")

    samples = []
    for idx, yaml_path in enumerate(traverse_metadata_yamls(directory), start=1):
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

    print(f"Calling Log Detective API: {full_api_url} for {len(samples)} samples...")

    analysis_results: dict[str, tuple[float, str]] = {}
    max_workers = min(32, len(samples))
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
            except ValueError as e:
                raise ValueError(
                    f"Could not decode JSON from API response for {sample_uuid}"
                ) from e
            except (KeyError, TypeError) as e:
                raise ValueError(
                    f"Could not find 'explanation' in API response for {sample_uuid}."
                ) from e

    for idx, sample in enumerate(samples, start=1):
        expected_issue: str = sample["expected_issue"]
        sample_uuid: str = sample["sample_uuid"]
        yaml_path: str = sample["yaml_path"]
        logs: list[str] = sample["log_files"]
        uuid_prefix = sample_uuid.split("-")[0]

        time_elapsed, actual_issue = analysis_results[sample_uuid]

        print(
            f"\n--- ({idx}) Analyzing {uuid_prefix} : {' '.join(logs)} ".ljust(80, "-")
        )
        print("\n[Expected Response]")
        print(expected_issue.strip())
        print("\n[Actual Response]")
        print(actual_issue.strip())

        try:
            score = get_similarity_score(
                expected_issue, actual_issue, client, llm_model
            )
        except (openai.APIError, openai.APIConnectionError) as e:
            raise ConnectionError(f"Cannot reach LLM judge at {llm_url}") from e
        except (ValidationError, KeyError, TypeError) as e:
            raise ValueError(
                f"Failed to parse similarity score for {sample_uuid}: {e}"
            ) from e

        scores.append(score)
        if score >= 6:
            samples_passing += 1
        elapsed_times.append(time_elapsed)

        print(
            f"\n[Judge] Similarity Score: {score}/10 Time elapsed: {time_elapsed:.3f}s"
        )

    median_score = median(scores)
    if elapsed_times:
        median_elapsed_time = median(elapsed_times)

    print(
        f"{samples_passing}/{len(scores)} samples pass, "
        f"Median score: {median_score}, "
        f"Median time: {median_elapsed_time:.3f}s."
    )


def main():
    """
    Main function to parse arguments and run the evaluation script.
    """
    parser = argparse.ArgumentParser(
        description="Evaluate AI system performance by comparing expected and actual responses.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--open-ai-api-key",
        help="Path to file with API key to OpenAI compatible inference provider",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--data-directory",
        help="Path to the directory containing the sample data.",
        default="./data",
    )
    parser.add_argument(
        "--log-detective-url",
        help="Base URL of the Log Detective server (e.g. http://localhost:8080).",
        required=True,
    )
    parser.add_argument(
        "--llm-url",
        help="URL of LLM API to use as judge (e.g. https://generativelanguage.googleapis.com/v1beta/openai/)",
        required=True,
    )
    parser.add_argument(
        "--llm-model",
        help="Name of LLM model to use a judge (e.g. gemini-2.5-flash)",
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
    args = parser.parse_args()

    open_ai_api_key = get_api_key_from_file(args.open_ai_api_key)

    if not os.path.isdir(args.data_directory):
        print(f"Error: Directory not found at '{args.data_directory}'", file=sys.stderr)
        sys.exit(1)

    log_detective_api_key = ""
    if args.log_detective_api_key:
        log_detective_api_key = get_api_key_from_file(args.log_detective_api_key)

    evaluate_samples(
        directory=args.data_directory,
        server_address=args.log_detective_url,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        llm_token=open_ai_api_key,
        log_detective_api_timeout=args.log_detective_api_timeout,
        log_detective_api_key=log_detective_api_key,
    )


if __name__ == "__main__":
    main()
