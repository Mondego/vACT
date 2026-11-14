#!/bin/bash

################################################################################
# vLLM Server Launcher for Qwen/Qwen2.5-Coder-32B-Instruct
# This script starts a vLLM OpenAI-compatible API server with optimal settings
################################################################################

set -e  # Exit on error

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

################################################################################
# Configuration
################################################################################

# Model configuration
MODEL_NAME="Qwen/Qwen2.5-Coder-32B-Instruct"
#MODEL_NAME="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ" # AQW

MODEL_DTYPE="bfloat16"  # Options: half, float16, bfloat16, float32

# Server configuration
HOST="0.0.0.0"
PORT=8000
API_KEY="specsyns"  # CHANGE THIS for production!

MAX_MODEL_LEN=32768           # Max sequence length (input + output)
GPU_MEMORY_UTILIZATION=0.90  # Use 90% of GPU memory
TENSOR_PARALLEL_SIZE=2       # Number of GPUs to use
CUDA_VISIBLE_DEVICES="6,7"   # Use nvidia-smi to check GPU usage and adjust accordingly (e.g., "0,1,2,3" for 4 GPUs)
export CUDA_VISIBLE_DEVICES

# Advanced options (usually don't need to change)
MAX_NUM_SEQS=512            # Max number of sequences to process in parallel
SWAP_SPACE=4                # CPU swap space in GB
ENFORCE_EAGER=false         # Set to true to disable CUDA graphs (slower but more compatible)



# Optional: Quantization (uncomment to use AWQ/GPTQ for better memory efficiency)
# QUANTIZATION="awq"  # Options: awq, gptq, squeezellm, or leave empty for none
#QUANTIZATION="awq"

# Optional: Hugging Face token (only needed for gated models)
# export HF_TOKEN="hf_your_token_here"

################################################################################
# Functions
################################################################################

print_header() {
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

print_info() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

check_dependencies() {
    print_header "Checking Dependencies"
    
    # Check if Python is installed
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed"
        exit 1
    fi
    print_info "Python: $(python3 --version)"
    
    # Check if vLLM is installed
    if ! python3 -c "import vllm" 2>/dev/null; then
        print_error "vLLM is not installed"
        echo ""
        echo "Install with: pip install vllm"
        exit 1
    fi
    print_info "vLLM is installed"
    
    # Check if CUDA is available
    if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
        print_warning "CUDA not available - vLLM requires GPU"
        exit 1
    fi
    
    local gpu_name=$(python3 -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null)
    local gpu_memory=$(python3 -c "import torch; print(f'{torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f}')" 2>/dev/null)
    print_info "GPU: $gpu_name (${gpu_memory}GB)"
    
    echo ""
}

check_port() {
    print_header "Checking Port Availability"
    
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        print_error "Port $PORT is already in use"
        echo ""
        echo "Kill the process using: sudo lsof -ti:$PORT | xargs kill -9"
        echo "Or choose a different port by editing this script"
        exit 1
    fi
    print_info "Port $PORT is available"
    echo ""
}

print_config() {
    print_header "Server Configuration"
    
    echo "Model:                 $MODEL_NAME"
    echo "Host:                  $HOST"
    echo "Port:                  $PORT"
    echo "API Key:               ${API_KEY:0:10}... (hidden)"
    echo "Data Type:             $MODEL_DTYPE"
    echo "Max Model Length:      $MAX_MODEL_LEN tokens"
    echo "GPU Memory Util:       ${GPU_MEMORY_UTILIZATION} (90%)"
    echo "Tensor Parallel Size:  $TENSOR_PARALLEL_SIZE"
    if [ -n "$QUANTIZATION" ]; then
        echo "Quantization:          $QUANTIZATION"
    fi
    echo ""
}

build_vllm_command() {
    local cmd="python3 -m vllm.entrypoints.openai.api_server"
    cmd="$cmd --model $MODEL_NAME"
    cmd="$cmd --host $HOST"
    cmd="$cmd --port $PORT"
    cmd="$cmd --dtype $MODEL_DTYPE"
    cmd="$cmd --max-model-len $MAX_MODEL_LEN"
    cmd="$cmd --gpu-memory-utilization $GPU_MEMORY_UTILIZATION"
    cmd="$cmd --tensor-parallel-size $TENSOR_PARALLEL_SIZE"
    cmd="$cmd --max-num-seqs $MAX_NUM_SEQS"
    cmd="$cmd --swap-space $SWAP_SPACE"
    cmd="$cmd --api-key $API_KEY"
    
    if [ -n "$QUANTIZATION" ]; then
        cmd="$cmd --quantization $QUANTIZATION"
    fi
    
    if [ "$ENFORCE_EAGER" = true ]; then
        cmd="$cmd --enforce-eager"
    fi
    
    echo "$cmd"
}

start_server() {
    print_header "Starting vLLM Server"
    
    local vllm_cmd=$(build_vllm_command)
    
    print_info "Command: $vllm_cmd"
    echo ""
    print_warning "Starting server... This may take 1-2 minutes to load the model"
    echo ""
    
    # Run vLLM
    eval $vllm_cmd
}

print_usage_instructions() {
    print_header "Server Started Successfully!"
    
    echo "API Endpoint: http://localhost:$PORT/v1"
    echo ""
    echo "Test with curl:"
    echo "  curl http://localhost:$PORT/v1/models"
    echo ""
    echo "Example Python client:"
    cat << 'EOF'
  from openai import OpenAI
  
  client = OpenAI(
      base_url="http://localhost:8000/v1",
      api_key="your-secret-key-change-me"
  )
  
  response = client.chat.completions.create(
      model="Qwen/Qwen2.5-Coder-32B-Instruct",
      messages=[
          {"role": "user", "content": "Write a Python hello world"}
      ]
  )
  
  print(response.choices[0].message.content)
EOF
    echo ""
    print_info "Press Ctrl+C to stop the server"
    echo ""
}

cleanup() {
    echo ""
    print_warning "Shutting down vLLM server..."
    exit 0
}

################################################################################
# Main Execution
################################################################################

main() {
    clear
    print_header "vLLM Server Launcher"
    echo ""
    
    # Run checks
    check_dependencies
    check_port
    print_config
    
    # Set up signal handling for graceful shutdown
    trap cleanup SIGINT SIGTERM
    
    # Start the server
    start_server
}

# Run main function
main
