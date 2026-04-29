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

Keys for OpenAI compatible LLM inference provider and Log Detective itself,
must be stored in files. These will be used at runtime.
This prevents logging of secrets in history.

Example:

```bash
./validation.py --open-ai-api-key OPENAI_API_KEY_FILE \
--data-directory ./path/to/samples \
--log-detective-url http://localhost:8080 \
--llm-url https://generativelanguage.googleapis.com/v1beta/openai/ \
--llm-model gemini-2.5-flash \
--log-detective-api-timeout 300 \
--log-detective-api-key LD_API_KEY_FILE
```

The only 3 required arguments are `--open-ai-api-key`, `--llm-url`, and `--llm-model`.

Script sends each of the the stored log files for evaluation by Log Detective,
then submits both results of final analysis from Log Detective and actual issue
in the log to LLM (judge) to determine the semantic similarity of the two.

Scores are assigned on scale from `1` to `10`. Where `10` stands for absolute and
`1` for no match at all.

Example:

```
[Expected Response]
Build failed due to missing patch file `gnome-shell-notify-gnome-session.patch`.
Fixing the issue requires making sure that all patch files specified are in the `SOURCES` directory.


[Actual Response]
The RPM build failed because the patch file `gnome-shell-notify-gnome-session.patch` was missing from the `SOURCES` directory during the `buildsrpm` phase. This caused the `rpmbuild -bs` command to fail.

To resolve this, ensure that the `gnome-shell-notify-gnome-session.patch` file is present in the `SOURCES` directory and is correctly referenced in the RPM spec file.


Similarity Score: 8/10 Time elapsed: 4.5s
--------------------------------------------------------------------------------
```

Scores higher or equal to 6 are considered sufficient for passing.
