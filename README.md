# Project Releases

Automated build and release management system for GitHub projects. This system builds projects (e.g., forks) via GitHub Actions and hosts a static website with the build history and artifact downloads.

## How it Works

1.  **Project Definitions**: Projects are defined in the `projects/` directory as JSON files.
2.  **Automated Builds**: A GitHub Action runs daily (or on push), scanning the `projects/` folder.
3.  **Environment Detection**: The system automatically detects if a project needs Java or Node.js and sets up the environment accordingly.
4.  **Persistent History**: Build metadata and artifacts are stored in the `gh-pages` branch, ensuring a full history is maintained.
5.  **Release Website**: A clean, static dashboard (`index.html`) lazy-loads the build history and provides direct download links.

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
1.  Prepare a `staging/` directory.
2.  Clone and build all projects in `.work/`.
3.  Collect artifacts into `staging/artifacts/`.
4.  Generate `projects.json` and individual build metadata.

To view the website locally, you can serve the `staging/` directory:

```bash
cd staging && python3 -m http.server 8000
```

## Structure

- `projects/`: Project configuration files.
- `scripts/`: Python orchestration scripts.
- `index.html`: The frontend dashboard template.
- `config.json`: General site configuration (Org/Repo info).
- `.github/workflows/`: CI/CD pipeline.
