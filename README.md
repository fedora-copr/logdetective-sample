# logdetective-sample

This repository contains logs of failed RPM builds that are interesting,
tricky, or complicated and serve as a benchmark for Log Detective.


## Sample format

All samples must be placed in the `./data/` path.
Name of the sample directory must be valid uuid4, for example generated using `uuid -v4`.
Sample must consist of original log files, without alterations or modifications,
and a `sample_metadata.yaml` file.

Log files must be in plain text, and not compressed.

The `sample_metadata.yaml` must contain the following fields (for the minimum necessary context understanding):
- `source_project_name` - what project is the sample from
- `issue` - primary issue causing the build failure (ground truth)
- `log_detective_version` - version of Log Detective used in the field
- `log_detective_analysis` - full analysis provided by Log Detective at the time in the field
- `log_files` - names of the log files making the sample, they need to be present in the same directory as the yaml file

For the bare evaluation of the current Log Detective analysis precision, only `issue` and `log_files` fields are actually needed.

The file may (optionally) also contain the following fields:
- `references` - with a list of URLs
- `notes` - with more information about the sample
- `api` - containing name of API endpoint used to obtain the analysis


### Example:

```yaml
source_project_name: firefox
source_project_version: x.y.z
issue: |
    Build failed due to unmet dependency.
log_detective_version: 1.0.4
log_detective_analysis:
    As a large language model I can not help you with diagnosing RPM build issues.
log_files:
    - build.log
    - root.log
references:
    - https://www.something.com
notes: |
    I tried to build it despite unfavorable configuration of the planets.
    Was I wrong?
api: /analyze/staged
```


## Automated evaluation

Evaluation of Log Detective performance can be performed automatically using
the `validation.py` script. Dependencies for the tool are defined in the
`requirements.txt` file and should be installed in a virtual environment.

Script requires access to `llama` binary (built from llama.cpp), with System One
support (b11820 or newer, tested for commit `5851a8e5` (Nov 4 2026)).
Make sure the binary is available on `PATH`. For instructions, see
[Llama installation instructions](https://llama-cpp.com/getting-started/), or
[llama.cpp build guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md).

Optionally, you can replace the default judge model using CLI arguments, linking
the HuggingFace repository. The repository must contain a System One compatible
decision model. For the judge API used by the validator, see:
[llama.cpp System One endpoint documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1systemone-typesafe-compatible-system-one-api).
Depending on the `--judge-model` you choose, you might need to modify `--judge-timeout`.

Key for Log Detective must be stored in a file, and will be used at runtime.
This prevents logging of secrets in history.

Example:
```bash
./validation.py \
--data-directory ./path/to/samples \
--judge-model org/repository:quant \
--judge-timeout 120 \
--log-detective-url http://localhost:8080 \
--log-detective-api-timeout 300 \
--log-detective-api-key LD_API_KEY_FILE \
2> llama.log
```

The only required argument is `--log-detective-url` - the base URL of
a reachable Log Detective server (not the endpoint).
Other parameters have defaults in place.
You can see what they do and how to override them using `./validation.py --help`.
`llama` writes to stderr, so filter it out, if necessary.

Script sends each of the the stored log files for evaluation by Log Detective,
then submits both results of final analysis from Log Detective and actual issue
in the log to LLM (judge) to determine the semantic similarity of the two.

Results are % probabilities of the two descriptions covering the same root cause
- 100 % meaning that the judge is certain that both analyses cover the same root cause.

## Output format

The judge returns a probability that the Log Detective analysis identifies the
same root cause and gives a maintainer enough information to act. System One's
`noul` field is the probability of yes.

Example:

```
[Expected Response]
Build failed due to missing patch file `gnome-shell-notify-gnome-session.patch`.
Fixing the issue requires making sure that all patch files specified are in the `SOURCES` directory.

[Actual Response]
The RPM build failed because the patch file `gnome-shell-notify-gnome-session.patch` was missing from the `SOURCES` directory during the `buildsrpm` phase. This caused the `rpmbuild -bs` command to fail.

[Judge] Similarity: 87.2 %, Response time: 4.515s, Judge time: 30.382s
--------------------------------------------------------------------------------
```

A sample passes when `P(yes) >= 0.5`.
Decisions from 0.45 through 0.55 are also marked low confidence; this does not
change the pass threshold.
Probabilities less than 0.05 or higher than 0.95 are marked as high confidence.
