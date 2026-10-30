# LLM Feedback on Novice Programs: Code Only vs. Code + Verified Signals

COMP 5112 Research Methodology, Lakehead University
Authors: Yash Suthar, Neel Patel

Does giving a large language model verified compiler and unit-test evidence make its feedback on novice Python programs more accurate than showing it the code alone?

This repository contains the full pipeline we used to answer that question: sample student submissions, run the code in a sandbox, ask an LLM for hints under two conditions, log everything, label the hints with two human coders, and test the difference.

## Study design

| | Condition A | Condition B |
|---|---|---|
| Model sees | Assignment + student code | Assignment + student code + **verification results** (interpreter errors, per-test PASS/FAIL, expected vs. actual values) |
| Model, prompt template, temperature, k | identical | identical |

- **Design:** within-subjects and paired. Every submission gets hints under both conditions.
- **Samples:** k = 3 hints per submission per condition, combined by majority vote.
- **Hint coding:** Silva & Costa taxonomy: `AC` (accurate and complete), `ALO` (accurate line only), `AIO` (accurate issue only), `FP` (false positive), `MS` (misleading suggestion). Our added rule: saying buggy code is correct is coded `MS`.
- **Label quality:** coder 1 labels every hint, coder 2 independently labels a random 25%. Cohen's kappa must reach 0.70 on the double-coded hints.
- **Test:** McNemar's exact test on the paired AC / not-AC outcomes.
- **Hypotheses:**
  - **H1:** Condition B produces a higher share of AC hints than Condition A.
  - **H2:** Condition B reduces false positives and misleading suggestions.

## Dataset

[`koutch/intro_prog`](https://huggingface.co/datasets/koutch/intro_prog) on Hugging Face: real novice Python submissions from introductory programming courses, each with the assignment description, unit-test assertions and a correct/buggy label. The notebook downloads it automatically and needs no access request. By default it uses the Singapore (NUS) subset (4,394 submissions).

The dataset is released under the LGPL-3.0 license. Please see its dataset page for details and citation information.

(The notebook can also load FalconCode or a CSV; see `DATA_SOURCE` in the config cell.)

## Files

| File | What it is |
|---|---|
| `falconcode_llm_feedback_pipeline (6).ipynb` | The full pipeline as a notebook: config, data loading, sampling, verification harness, LLM calls for A and B, annotation sheet export, kappa and McNemar analysis |
| `feedback_app_.py` | A desktop viewer (tkinter, standard library only) for inspecting every logged prompt, output and A-vs-B comparison, plus a live tab for trying a new submission |

## Setup

Requirements: Python 3.10+ and an OpenAI API key (or an Anthropic key, or use the free mock model).

```bash
pip install pandas numpy tqdm scikit-learn statsmodels matplotlib huggingface_hub openai python-dotenv
```

Put your API key in a `.env` file next to the notebook. **Never commit this file.**

```
OPENAI_API_KEY=your_key_here
```

Add `.env` to your `.gitignore`.

## Running the pipeline

1. Open the notebook and review the **Config** cell. The main settings:
   - `DATA_SOURCE = "intro_prog"`, `IP_SOURCE = "singapore"`
   - `N_PROBLEMS = 5`, `PER_PROBLEM = 10`, `FRAC_CORRECT = 0.2`
   - `LLM_PROVIDER = "openai"` (or `"anthropic"` or `"mock"`), `OPENAI_MODEL = "gpt-4o"`
   - `TEMPERATURE = 0.0`, `K_SAMPLES = 3`
   - `OUTPUT_DIR`: a folder you can write to
2. Run the cells in order. The notebook:
   - loads and samples the data,
   - runs the verification harness on each submission,
   - makes the LLM calls and stores every prompt and raw output in `feedback.db` (SQLite),
   - writes `annotation_sheet.csv` and `category_guide.csv`.
3. Label the sheet: coder 1 fills `category_coder1` for every row, coder 2 fills `category_coder2` for the rows with `coder2_subset = 1`, and any disagreements are resolved in `final_category`.
4. Set `ANNOT_CSV` to the labelled sheet and run the last cell. It reports Cohen's kappa, the majority vote per submission, McNemar's test, the category distribution, and saves `results.json` and `category_distribution.png`.

Set `LLM_PROVIDER = "mock"` to test the whole pipeline without spending anything. The mock model's results are not meaningful.

## Viewer app

```bash
python feedback_app_.py path/to/feedback.db
```

- **Browse results:** open a run to see the student code, the verification evidence, the exact prompt and the model's output. "Compare A vs B" shows both conditions for one submission.
- **Try it live:** paste an assignment and code, choose a condition and provider, and watch the prompt and the answer appear.

## Safety note

The harness runs untrusted student code in a separate process with a 5-second timeout and an AST blocklist for risky imports and calls. On Linux it also applies CPU, memory and file-size limits. On Windows only the timeout applies, and it is not a Docker container. Use it for study samples of known coursework, not for production.

## Results (first run)

GPT-4o, 50 submissions from 5 problems, 300 calls, 30 submissions human-coded:

| | A (code only) | B (code + signals) |
|---|---|---|
| AC hints | 53% | 100% |

- 14 submissions were AC only under B and none were AC only under A (McNemar exact p < 0.001).
- Under A, 47% of hints were false positives; under B, 0%.
- Cohen's kappa: 0.79 (5 categories), 0.78 (AC vs. not AC).

## Limitations

- Small purposive sample from one course and one model.
- Condition B sees the same tests that define whether code is correct, so part of its advantage is by design.
- At temperature 0 the three samples are nearly identical.
- Human coding covers 30 of 50 submissions.
- In the first run, the safety filter blocked submissions that call `list.remove`, so for 15 of 50 submissions B's interpreter check did not run. This is fixed in the updated notebook.
- Weaker sandbox than Docker, especially on Windows.

## Acknowledgements

- Dataset: [koutch/intro_prog](https://huggingface.co/datasets/koutch/intro_prog)
- Error taxonomy: Silva & Costa
