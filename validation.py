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
from pathlib import Path
from statistics import median
from typing import Generator

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

    prompt = f"""
    Analyze the semantic similarity between the 'expected_output' and the 'actual_output'.

    Your task is to rate their similarity on an integer scale from 1 to 10.
    - A score of 1 means they are completely dissimilar in meaning, topic, and intent.
    - **A score of 7-9 means the actual output contains all the critical information of the expected output, but also includes additional, relevant explanations or details.**
    - A score of 10 means they are semantically identical, conveying the exact same information and intent, even if phrasing differs.

    ---
    "expected_output": "{expected_text}"
    "actual_output": "{actual_text}"
    """
    response = llm_client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "user", "content": prompt},
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
    api_endpoint = "/analyze/staged"

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

    for yaml_path in traverse_metadata_yamls(directory):
        print(f"--- Processing: {yaml_path} ---")

        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                metadata: dict = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Could not parse {yaml_path}: {e}") from e

        if not isinstance(metadata, dict):
            raise TypeError(f"Unexpected YAML structure of {yaml_path}")

        expected_issue = metadata.get("issue")
        log_files = metadata.get("log_files")
        sample_uuid = yaml_path.split("/")[-2] # ... data / uuid [-2] / sample_metadata.yaml [-1]

        if not expected_issue or not log_files:
            raise ValueError(f"Invalid {yaml_path}: missing 'issue' or 'log_files' field.")

        payload = create_payload_from_yaml(log_files, yaml_path)

        actual_response_data = None
        try:
            print(f"Calling Log Detective API: {full_api_url}")
            print(f"Request contains logs from {sample_uuid}: {log_files}")
            start_time = time.time()
            api_response = requests.post(
                full_api_url,
                json=payload,
                timeout=log_detective_api_timeout,
                headers=log_detective_request_headers,
            )
            api_response.raise_for_status()
            actual_response_data = api_response.json()
            time_elapsed = time.time() - start_time
            # Extract the text from the 'explanation' object based on the provided schema
            actual_issue = actual_response_data["explanation"]["text"]
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.HTTPError,
        ) as e:
            raise ConnectionError(
                f"Could not obtain Log Detective response at {full_api_url}: {e}"
            ) from e
        except ValueError as e:
            raise ValueError(f"Could not decode JSON from API response for {sample_uuid}") from e
        except (KeyError, TypeError) as e:
            raise ValueError(
                f"Could not find 'explanation.text' in API response "
                f"for {sample_uuid}. Response: {actual_response_data}"
            ) from e

        print("\n[Expected Response]")
        print(expected_issue)
        print("\n[Actual Response]")
        print(actual_issue)

        try:
            score = get_similarity_score(
                expected_issue, actual_issue, client, llm_model
            )
        except (openai.APIError, openai.APIConnectionError) as e:
            raise ConnectionError(f"Cannot reach LLM judge at {llm_url}") from e
        except (ValidationError, KeyError, TypeError) as e:
            raise ValueError(f"Failed to parse similarity score for {sample_uuid}: {e}") from e

        scores.append(score)
        if score >= 6:
            samples_passing += 1
        elapsed_times.append(time_elapsed)

        print(f"\nSimilarity Score: {score}/10 Time elapsed: {time_elapsed:.3f}s")
        print("-" * (len(yaml_path) + 18))
        print("\n")

    if scores:
        median_score = median(scores)
    else:
        raise ValueError("No samples found.")
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
        "--llm-url", help="URL of LLM API to use as judge (e.g. https://generativelanguage.googleapis.com/v1beta/openai/)", required=True
    )
    parser.add_argument(
        "--llm-model", help="Name of LLM model to use a judge (e.g. gemini-2.5-flash)", required=True
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
