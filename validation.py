#!/usr/bin/env python3

import os
import sys
import requests
import yaml
import openai
import argparse
from pydantic import BaseModel, Field, ValidationError

# --- Configuration ---
# Set your OpenAI API key as an environment variable named OPENAI_API_KEY
# You can get a key from https://beta.openai.com/account/api-keys
API_KEY = os.getenv("OPENAI_API_KEY")
LOG_REPO_BASE_URL = (
    "https://raw.githubusercontent.com/fedora-copr/logdetective-sample/main/data/"
)


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
        int: A similarity score from 1 to 10, or None if an error occurs.
    """

    prompt = f"""
    You are an AI performance evaluator. Your task is to compare two text snippets and rate their similarity on a scale of 1 to 10, where 1 is completely dissimilar and 10 is identical or semantically equivalent.
    Provide only the integer score in your response.

    Expected Response:
    ---
    {expected_text}
    ---

    Actual Response:
    ---
    {actual_text}
    ---

    Similarity Score (1-10):
    """

    try:
        response = llm_client.chat.completions.create(
            model=llm_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that provides similarity scores.",
                },
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
    except openai.APIError as e:
        print(f"Error calling OpenAI API: {e}", file=sys.stderr)
        raise e
    content = response.choices[0].message.content
    if not isinstance(content, str):
        print(f"Invalid response from LLM {content}")
        raise TypeError
    try:
        score = SimilarityScore.model_validate_json(content)
    except ValidationError as e:
        print(
            f"Error: Could not parse the score from the LLM response: '{content}'",
            file=sys.stderr,
        )
        raise e

    return score.score


def evaluate_samples(
    directory: str, server_address: str, llm_url: str, llm_model: str, llm_token: str,
    log_detective_api_timeout: int,
) -> None:
    """
    Traverses a directory to find and evaluate log analysis samples.

    Args:
        directory (str): The path to the directory containing the samples.
        server_address (str): The base address of the server.
    """
    api_endpoint = "/analyze/staged"

    full_api_url = f"{server_address}{api_endpoint}"

    client = openai.OpenAI(base_url=llm_url, api_key=llm_token)
    for root, _, files in os.walk(directory):
        for file in files:
            if file == "sample_metadata.yaml":
                yaml_path = os.path.join(root, file)
                print(f"--- Processing: {yaml_path} ---")

                try:
                    with open(yaml_path, "r") as f:
                        metadata = yaml.safe_load(f)
                except yaml.YAMLError as e:
                    print(f"Error parsing YAML file {yaml_path}: {e}", file=sys.stderr)
                    continue

                expected_issue = metadata.get("issue")
                log_file_name = metadata.get("log_file")
                sample_uuid = os.path.basename(root)

                if not expected_issue or not log_file_name:
                    print(
                        f"Skipping {yaml_path}: missing 'issue' or 'log_file' field.",
                        file=sys.stderr,
                    )
                    continue

                log_file_url = f"{LOG_REPO_BASE_URL}{sample_uuid}/{log_file_name}"
                payload = {"url": log_file_url}
                actual_response_data = None
                try:
                    print(
                        f"Calling Log Detective API: {full_api_url} with log file URL: {log_file_url}"
                    )
                    api_response = requests.post(full_api_url, json=payload, timeout=log_detective_api_timeout)
                    api_response.raise_for_status()
                    actual_response_data = api_response.json()
                    # Extract the text from the 'explanation' object based on the provided schema
                    actual_issue = actual_response_data["explanation"]["text"]
                except requests.exceptions.RequestException as e:
                    print(f"Error calling API for {log_file_url}: {e}", file=sys.stderr)
                    continue
                except ValueError:
                    print(
                        f"Error: Could not decode JSON from API response for {log_file_url}",
                        file=sys.stderr,
                    )
                    continue
                except (KeyError, TypeError):
                    print(
                        f"Error: Could not find 'explanation.text' in API response for {log_file_url}. Response: {actual_response_data}",
                        file=sys.stderr,
                    )
                    continue

                print("\n[Expected Response]")
                print(expected_issue)
                print("\n[Actual Response]")
                print(actual_issue)

                try:
                    score = get_similarity_score(
                        expected_issue, actual_issue, client, llm_model
                    )
                except (openai.APIError, ValidationError, TypeError) as e:
                    print(
                        f"Failed to retrieve similarity score with {e}", file=sys.stderr
                    )
                    continue

                print(f"\nSimilarity Score: {score}/10")

                print("-" * (len(yaml_path) + 18))
                print("\n")


def main():
    """
    Main function to parse arguments and run the evaluation script.
    """
    parser = argparse.ArgumentParser(
        description="Evaluate AI system performance by comparing expected and actual responses.",
    )
    parser.add_argument(
        "data_directory", help="Path to the directory containing the sample data."
    )
    parser.add_argument(
        "logdetective_url",
        help="Base URL of the Log Detective server (e.g., http://localhost:8080).",
    )
    parser.add_argument("llm_url", help="URL of LLM API to use as judge")
    parser.add_argument("llm_model", help="Name of LLM model to use a judge")
    parser.add_argument("log_detective_api_timeout", help="Request timeout for Log Detective API", type=int, default=60)
    args = parser.parse_args()

    if not API_KEY:
        print("Error: OPENAI_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(args.data_directory):
        print(f"Error: Directory not found at '{args.data_directory}'", file=sys.stderr)
        sys.exit(1)

    evaluate_samples(
        args.data_directory,
        args.logdetective_url,
        args.llm_url,
        args.llm_model,
        API_KEY,
        args.log_detective_api_timeout,
    )


if __name__ == "__main__":
    main()
