#!/usr/bin/env bash

# Pure validation helpers shared by run_e1.sh and its regression test. This file
# performs no work when sourced.

require_e1_run_label() {
    local run_label="$1"
    if [[ -z "$run_label" ]]; then
        echo "ERROR: This benchmark profile requires a nonempty E1_RUN_LABEL."
        return 1
    fi
}

validate_e1_run_policy() {
    local config="$1"
    local benchmark_basename="$2"
    local run_type="$3"
    local run_label="$4"

    case "$benchmark_basename" in
        benchmark.yaml)
            if [[ "$run_type" != "fixed-profile" ]]; then
                echo "ERROR: benchmark.yaml requires E1_RUN_TYPE=fixed-profile."
                return 1
            fi
            ;;
        benchmark_sphincs_sweep.yaml|benchmark_sphincs_refine_3_4.yaml|benchmark_sphincs_boundary_35.yaml|benchmark_sphincs_boundary_28.yaml|benchmark_sphincs_boundary_24.yaml|benchmark_sphincs_boundary_22.yaml|benchmark_sphincs_boundary_23.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "sphincs" || "$run_type" != "sweep" ]]; then
                echo "ERROR: SPHINCS+ sweep profiles require config=sphincs and E1_RUN_TYPE=sweep."
                return 1
            fi
            ;;
        benchmark_tx_evidence_50.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ ! "$config" =~ ^(ecdsa|ml-dsa-44|ml-dsa-65)$ || "$run_type" != "evidence" ]]; then
                echo "ERROR: The transaction-evidence profile requires config=ecdsa|ml-dsa-44|ml-dsa-65 and E1_RUN_TYPE=evidence."
                return 1
            fi
            ;;
        benchmark_blockutil.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ecdsa" || "$run_type" != "diagnostic" ]]; then
                echo "ERROR: The 300-TPS block diagnostic requires config=ecdsa and E1_RUN_TYPE=diagnostic."
                return 1
            fi
            ;;
        benchmark_ecdsa_sustained_222.yaml|benchmark_ecdsa_sustained_223.yaml|benchmark_ecdsa_sustained_224.yaml|benchmark_ecdsa_sustained_227.yaml|benchmark_ecdsa_sustained_228.yaml|benchmark_ecdsa_sustained_229.yaml|benchmark_ecdsa_sustained_230.yaml|benchmark_ecdsa_sustained_237.yaml|benchmark_ecdsa_sustained_250.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ecdsa" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: ECDSA sustainability profiles require config=ecdsa and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_200.yaml|benchmark_ml_dsa_sustained_250.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ ! "$config" =~ ^(ml-dsa-44|ml-dsa-65)$ || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA 200-TPS profile requires config=ml-dsa-44|ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_65_sustained_250.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-65" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-65 250-TPS profile requires config=ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_65_sustained_300.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-65" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-65 300-TPS profile requires config=ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_65_sustained_350.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-65" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-65 350-TPS profile requires config=ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_65_sustained_400.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-65" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-65 400-TPS profile requires config=ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_65_sustained_450.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-65" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-65 450-TPS profile requires config=ml-dsa-65 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_300.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 300-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_350.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 350-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_356.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 356-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_357.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 357-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_358.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 358-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_359.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 359-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_360.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 360-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_361.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 361-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_362.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 362-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_368.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 368-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_375.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 375-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_381.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 381-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_387.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 387-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_393.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 393-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_ml_dsa_sustained_400.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "ml-dsa-44" || "$run_type" != "sustainability" ]]; then
                echo "ERROR: The ML-DSA-44 400-TPS profile requires config=ml-dsa-44 and E1_RUN_TYPE=sustainability."
                return 1
            fi
            ;;
        benchmark_sphincs_blockutil_54.yaml)
            require_e1_run_label "$run_label" || return 1
            if [[ "$config" != "sphincs" || "$run_type" != "diagnostic" ]]; then
                echo "ERROR: The SPHINCS+ 54-TPS block diagnostic requires config=sphincs and E1_RUN_TYPE=diagnostic."
                return 1
            fi
            ;;
        *)
            echo "ERROR: Benchmark profile is not in the E1 scientific allowlist: $benchmark_basename"
            return 1
            ;;
    esac
}

ensure_e1_namespace_available() {
    local raw_dir="$1"
    local run_namespace="$2"
    local -a matches=()

    shopt -s nullglob
    matches=("$raw_dir/${run_namespace}_"*)
    shopt -u nullglob
    if (( ${#matches[@]} > 0 )); then
        echo "ERROR: Existing raw artifacts found for run namespace: $run_namespace"
        echo "Refusing to overwrite or mix measurement runs:"
        printf '%s\n' "${matches[@]}" | sort
        return 1
    fi
}
