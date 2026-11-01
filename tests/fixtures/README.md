# Test fixtures

Every file here is a small excerpt used by offline tests. The default test suite never contacts these sources.

| Files | Source | Terms |
|---|---|---|
| `epoch/*.csv` | Epoch AI, "Capabilities & benchmarking", https://epoch.ai/benchmarks, downloaded from `https://epoch.ai/data/benchmark_data.zip` on 2026-11-01 and filtered to a few dozen models | CC BY 4.0. Attribution: Epoch AI, 'Capabilities & benchmarking', https://epoch.ai/benchmarks |
| `modelsdev_*.json` | https://models.dev/api.json, filtered by model name | The models.dev project is MIT licensed |
| `openrouter_*.json` | `https://openrouter.ai/api/v1/models`, filtered by model id and reduced to id, name, creation time, context length, prices and supported parameters | Public model listing; excerpts are kept only to test parsing |
| `artificial_analysis_models.json` | Constructed by hand from the example in the Artificial Analysis OpenAPI document (`https://artificialanalysis.ai/api/v2/openapi`); it is not live data | Example from the published API description |
| `mcp_probe_server.py` | Written for this project | MIT, same as the repository |

Prices and scores in these files are a snapshot and will drift from the live sources. If you are a data owner and want an excerpt changed or removed, open an issue.
