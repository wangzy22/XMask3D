#!/bin/sh
set -x 

while [ "$#" -gt 0 ]; do
    case "$1" in
        --exp_dir=*)
            exp_dir="${1#*=}"
            ;;
        --config=*)
            config="${1#*=}"
            ;;
        *)
            echo "Unknown parameter: $1"
            exit 1
            ;;
    esac
    shift
done

if [ -z "$exp_dir" ] || [ -z "$config" ]; then
    echo "Usage: sh run/resume.sh --exp_dir=XX --config=XX"
    exit 1
fi

model_dir=${exp_dir}/model

mkdir -p "${exp_dir}"

export PYTHONPATH=.
python -u run/train.py \
  --config="${config}" \
  save_path "${exp_dir}" \
  resume "${model_dir}/model_last.pth.tar" \
  2>&1 | tee "${exp_dir}/resume-$(date +"%Y%m%d_%H%M").log"
