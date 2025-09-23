# Synthetic AML Dataset Generator

This project generates small-scale, realistic anti-money laundering (AML) datasets using synthetic transaction patterns. Designed for educational use, the output is suitable for Excel-based analysis and classroom exercises.

---

## Features

- Generate financial transactions with realistic timestamps, payment types, and currencies.
- Simulate common laundering patterns such as fan-out, cycles, and scatter-gather.
- Label laundering transactions for easy classification.
- Export clean `.csv` or `.xlsx` files for Excel.

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/your-org/aml-synthetic-generator.git
cd aml-synthetic-generator
```

### 2. Create a Virtual Environment

Make sure that you have Python 3.10 installed.

```bash
python3 -m venv aml-env
source aml-env/bin/activate 
```
### 3. Install Dependencies

```bash
pip3 install -r requirements.txt
```

# Files
### Patterns.yaml
The patterns.yaml file contains the instructions for each money laundering technique.

| Field                     | Description                                                                                         |
| ------------------------- | --------------------------------------------------------------------------------------------------- |
| `type`                    | Type of laundering pattern (`cycle`, `fan_out`, `fan_in`, `scatter_gather`, `gather_scatter`, etc.) |
| `instances`               | How many times to inject this pattern                                                               |
| `amount` / `total_amount` | Total amount to launder or per transaction                                                          |
| `accounts_per_*`          | How many accounts are involved (varies by pattern)                                                  |
| `currency`                | Optional override from default                                                                      |
| `start_date`, `end_date`  | Timestamp range to assign to transactions                                                           |
| `label`                   | Whether transactions should be marked as laundering                                                 |

### Agent Profiles
If you have a structured Excel file of agent profiles, you can generate transactions based on that data:

```bash
python main.py --agent_profiles agents/agent_profiles.xlsx
```

The generator will read merchant patterns, frequencies, payment methods, and average expenses to create realistic transactions between the entities defined in the file. Use the `--legit_txns` flag to cap how many baseline (pre-split) transactions are emitted from the profile data—this makes the profile-driven run respond to the same volume controls as the synthetic generator.

### Customizing the output file

You are not locked into the default `data/aml_dataset.xlsx` export. Pass a different file path (and optionally a different format) on the command line and the script will create the file in that location—intermediate folders are created automatically.

```bash
# Write an Excel workbook straight to the project root
python main.py \
  --agent_profiles agents/agent_profiles.xlsx \
  --legit_txns 10000 \
  --laundering_chains 3 \
  --individuals 4 \
  --companies 10 \
  --start_date 2025-01-01 \
  --end_date 2025-04-01 \
  --output testdata001.xlsx
```

To generate a CSV instead, change both the extension and the `--format` flag:

```bash
python main.py \
  --agent_profiles agents/agent_profiles.xlsx \
  --output exports/run_001/transactions.csv \
  --format csv
```

After the command finishes you will find the file at the requested path. The exporter automatically uses your provided name and folder, so any edits to `--output` take effect immediately.

### BEnt Entities (ATMs/Tellers)
`BEnt` rows in the agent profiles represent bank entities such as ATMs or teller locations. They provide the IDs and addresses used when cash withdrawals and deposits occur. Be sure to include them in the profile data so cash transactions can reference the correct location. If no `BEnt` information is provided, the generator will create placeholder ATMs.

