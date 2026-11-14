#!/usr/bin/env python3
"""
Test script for Qwen2.5-Coder-7B-Instruct served via vLLM OpenAI API
"""

from openai import OpenAI
import argparse
import sys
import time
import concurrent.futures

# Supported models
ALLOWED_MODELS = {
    "Qwen/Qwen2.5-Coder-32B-Instruct": "code-specialized, non-reasoning",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B": "reasoning",
    "Qwen/Qwen3-32B": "general instruct, non-reasoning",
}


def parse_args():
    model_list = "\n  ".join(f"{k}  ({v})" for k, v in ALLOWED_MODELS.items())
    parser = argparse.ArgumentParser(
        description="Test vLLM OpenAI API",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--model",
        required=True,
        choices=list(ALLOWED_MODELS),
        metavar="MODEL",
        help=f"Model to test. Allowed:\n  {model_list}",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000/v1", help="vLLM base URL"
    )
    parser.add_argument(
        "--api-key", default="specsyns", help="API key (default: specsyns)"
    )
    return parser.parse_args()


_args = parse_args()
VLLM_BASE_URL = _args.base_url
API_KEY = _args.api_key
MODEL_NAME = _args.model


def test_single_request():
    """Test a single chat completion request"""
    print("\n" + "=" * 60)
    print("TEST 1: Single Chat Completion Request")
    print("=" * 60)

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=API_KEY)

    start_time = time.time()

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "You are a helpful coding assistant."},
            {
                "role": "user",
                "content": "Write a Python function to calculate the Fibonacci sequence up to n terms.",
            },
        ],
        max_tokens=512,
        temperature=0.7,
        stream=False,
    )

    elapsed = time.time() - start_time

    print(f"\n✓ Response received in {elapsed:.2f}s")
    print(f"\nPrompt tokens: {response.usage.prompt_tokens}")
    print(f"Completion tokens: {response.usage.completion_tokens}")
    print(f"Total tokens: {response.usage.total_tokens}")
    print(f"\n--- Generated Code ---\n")
    print(response.choices[0].message.content)
    print("\n" + "-" * 60)


def test_streaming_request():
    """Test streaming response"""
    print("\n" + "=" * 60)
    print("TEST 2: Streaming Chat Completion")
    print("=" * 60)

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=API_KEY)

    print("\n--- Streaming Response ---\n")

    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "You are a helpful coding assistant."},
            {
                "role": "user",
                "content": "Explain what a decorator is in Python in 2-3 sentences.",
            },
        ],
        max_tokens=256,
        temperature=0.7,
        stream=True,
    )

    start_time = time.time()
    full_response = ""

    for chunk in stream:
        if chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            print(content, end="", flush=True)
            full_response += content

    elapsed = time.time() - start_time
    print(f"\n\n✓ Stream completed in {elapsed:.2f}s")
    print("-" * 60)


def single_concurrent_request(prompt_id):
    """Helper function for concurrent testing"""
    client = OpenAI(base_url=VLLM_BASE_URL, api_key=API_KEY)

    prompts = [
        "Write a Python function to reverse a string.",
        "Create a binary search implementation in Python.",
        "Write a quicksort algorithm in Python.",
        "Implement a simple linked list class in Python.",
        "Create a function to check if a string is a palindrome.",
    ]

    start_time = time.time()

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": "You are a coding assistant. Be concise."},
            {"role": "user", "content": prompts[prompt_id % len(prompts)]},
        ],
        max_tokens=256,
        temperature=0.7,
    )

    elapsed = time.time() - start_time

    return {
        "id": prompt_id,
        "elapsed": elapsed,
        "tokens": response.usage.total_tokens,
        "content_preview": response.choices[0].message.content[:100] + "...",
    }


def test_concurrent_requests(num_requests=5):
    """Test multiple concurrent requests"""
    print("\n" + "=" * 60)
    print(f"TEST 3: {num_requests} Concurrent Requests")
    print("=" * 60)

    start_time = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = [
            executor.submit(single_concurrent_request, i) for i in range(num_requests)
        ]
        results = [
            future.result() for future in concurrent.futures.as_completed(futures)
        ]

    total_elapsed = time.time() - start_time

    print(f"\n✓ All {num_requests} requests completed in {total_elapsed:.2f}s")
    print(f"\nIndividual request times:")

    for result in sorted(results, key=lambda x: x["id"]):
        print(
            f"  Request {result['id']}: {result['elapsed']:.2f}s ({result['tokens']} tokens)"
        )

    avg_time = sum(r["elapsed"] for r in results) / len(results)
    print(f"\nAverage per-request latency: {avg_time:.2f}s")
    print(f"Throughput: {num_requests / total_elapsed:.2f} requests/second")
    print("-" * 60)


def test_code_completion():
    """Test code completion capabilities"""
    print("\n" + "=" * 60)
    print("TEST 4: Code Completion Task")
    print("=" * 60)

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=API_KEY)

    code_prompt = """
Complete this Python function:

def merge_sort(arr):
    '''
    Implement merge sort algorithm
    Args:
        arr: List of comparable elements
    Returns:
        Sorted list
    '''
"""

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": "You are an expert Python programmer. Complete the code accurately.",
            },
            {"role": "user", "content": code_prompt},
        ],
        max_tokens=512,
        temperature=0.3,  # Lower temperature for more deterministic code
    )

    print("\n--- Generated Code Completion ---\n")
    print(response.choices[0].message.content)
    print("\n" + "-" * 60)


def check_server_health():
    """Check if vLLM server is running and accessible"""
    print("\n" + "=" * 60)
    print("Server Health Check")
    print("=" * 60)

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=API_KEY)

    try:
        models = client.models.list()
        print("\n✓ Server is running")
        print(f"\nAvailable models:")
        for model in models.data:
            print(f"  - {model.id}")
        return True
    except Exception as e:
        print(f"\n✗ Server connection failed: {e}")
        print(f"\nMake sure vLLM is running:")
        print(f"  python -m vllm.entrypoints.openai.api_server \\")
        print(f"    --model {MODEL_NAME} \\")
        print(f"    --port 8000 \\")
        print(f"    --api-key {API_KEY}")
        return False


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print(" vLLM Testing Suite")
    print("=" * 60)

    # Check server health first
    if not check_server_health():
        return

    # Run tests
    try:
        test_single_request()
        test_streaming_request()
        test_concurrent_requests(num_requests=5)
        test_code_completion()

        print("\n" + "=" * 60)
        print("✓ All tests completed successfully!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
