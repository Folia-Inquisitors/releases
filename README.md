# Project Releases

Automated build and release management system for GitHub projects. This system builds projects (e.g., forks) via GitHub Actions and hosts a static website with the build history and artifact downloads.

## System Requirements

To run the build system locally or in CI, you need:

- **Docker**: For isolated, reproducible builds.
- **Python 3.12+**: With [`uv`](https://github.com/astral-sh/uv) installed for dependency management.
- **Git**: For repository cloning and management.

## How it Works

1.  **Project Definitions**: Projects are defined in the `projects/` directory as JSON files.
2.  **Automated Builds**: A GitHub Action runs daily (or on push), scanning the `projects/` folder.
3.  **Docker Isolation**: Each project is built inside a dedicated Docker container (Maven or Node.js) to ensure maximum reproducibility and isolation.
4.  **Persistent History**: Build metadata and artifacts persist on the `gh-pages` branch via incremental commits — each run commits only changed files (`builds/*.json`, new `artifacts/**`, plus `projects.json`/`index.html` when actually changed), never a full-site re-upload.
5.  **Release Website**: A clean, structured dashboard (`index.html`) lazy-loads the build history and provides direct download links. The site is branch-based Pages served from `gh-pages`; runs that find no newer upstream commits push nothing.

## Getting Started (Use this for your own projects)

If you want to use this system for your own project releases:

1.  **Fork this repository**.
2.  **Update `config.json`**: Set your GitHub organization/user and repository name.
3.  **Configure Projects**: Delete the example files in `projects/` and add your own JSON configurations.
4.  **Enable GitHub Pages**:
    *   Go to your repository **Settings** > **Pages**.
    *   Set the source to **Deploy from a branch**, branch `gh-pages`, folder `/ (root)`.
    *   The `gh-pages` branch is created by the first workflow run, not by an Action deploy.
5.  **Trigger the first build**: Go to **Actions**, select **Build Project Releases**, and click **Run workflow**.

## Adding a New Project

To add a new project to the build pipeline, create a new JSON file in the `projects/` directory:

```json
{
  "name": "My Plugin",
  "repository": "Owner/Repo",
  "branch": "master",
  "build_command": "mvn clean package -B",
  "artifact_pattern": "target/*.jar",
  "setup": "java",
  "java_version": "17"
}
```

### Configuration Options
- `name`: Display name of the project.
- `repository`: GitHub repository (short form `Owner/Repo` or full URL).
- `branch`: The branch to build.
- `build_command`: The command to run to build the project.
- `artifact_pattern`: Glob pattern to find the resulting artifact (e.g., `target/*.jar`).
- `setup`: `java` or `node` (determines the build environment).
- `java_version` / `node_version`: Specific version required.
- `archived`: Set to `true` to display as archived and skip future builds.

## Local Development

You can run the build script locally using `uv`:

```bash
uv run scripts/build.py
```

This will:
1.  Prepare a `staging/` directory (the default `--site-dir`).
2.  Clone and build all projects in `.work/` — skipping projects whose upstream branch has no newer commit than the last successful recorded build.
3.  Collect artifacts into `staging/artifacts/`.
4.  Generate `projects.json` and individual build metadata.

For targeted runs, use `--site-dir` to pick the output directory and `--only <id>` (repeatable) to build a single project:

```bash
uv run scripts/build.py --site-dir /tmp/site --only my-project
```

To view the website locally, you can serve the output directory:

```bash
cd staging && python3 -m http.server 8000
```

## Structure

- `projects/`: Project configuration files.
- `scripts/`: Python orchestration scripts.
- `index.html`: The frontend dashboard template.
- `config.json`: General site configuration (Org/Repo info).
- `.github/workflows/`: CI/CD pipeline.
