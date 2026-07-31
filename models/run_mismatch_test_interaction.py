"""
run_mismatch_test_interaction.py
Entry point: tests variant_mismatch_interaction.py against the EPA-only
baseline via run_mismatch_variant_test.py's pre-registered bucket-focused
acceptance criteria.

Usage: python models/run_mismatch_test_interaction.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import variant_mismatch_interaction
from run_mismatch_variant_test import run_variant_test

if __name__ == "__main__":
    run_variant_test(
        challenger_feature_fn=variant_mismatch_interaction.features,
        challenger_predict_fn=variant_mismatch_interaction.predict_margin,
        challenger_label="EPA + mismatch interaction term (threshold=0.30)",
    )
