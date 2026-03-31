# IGV Desktop Proxy

A lightweight FastAPI-based reverse proxy designed to facilitate [IGV (Integrative Genomics Viewer)](https://igv.org/) Desktop's access to files stored in Google Cloud Storage (GCS).

## Local Development

This project uses [`uv`](https://docs.astral.sh/uv/) for fast Python dependency management.

### Prerequisites

- [uv](https://docs.astral.sh/uv/) installed on your system.
- Python 3.11+.

### Setup

1. **Clone the repository**:

    ```bash
    git clone git@github.com:populationgenomics/igv-desktop-proxy.git
    cd igv-desktop-proxy
    ```

2. **Install dependencies**:

    ```bash
    uv sync
    ```

3. **Run the local server**:

    ```bash
    uv run uvicorn --port 8080 --host localhost server.main:app
    ```

    The server will start at `http://localhost:8080`.

### Pulumi Setup

The pulumi scripts related to the GCP infrastructure is the `infrastructure` directory.
For the `dev` and `prod` environments. Set the following env variables to work.

```bash
export PULUMI_CONFIG_APP_DOMAIN="<app-domain-here>"
export PULUMI_CONFIG_GCP_PROJECT="<gcp-project-here>"
export PULUMI_CONFIG_GCP_REGION="australia-southeast1"
```

## Testing the IGV Desktop Python Proxy Locally

Follow the steps below to test the IGV desktop Python proxy locally.

### 1. Install IGV Desktop

[Install the IGV desktop application](https://igv.org/doc/desktop/):
Launch IGV once the installation is complete.

### 2. Configure Google Login in IGV

- Open **IGV Desktop**
- Go to:
  - View → Preferences → General
  - Enable Google access
- Click **Save changes**

After saving, a new **Google** tab will appear.

- Navigate to:
  - Google → Login
- Follow the Google authentication prompts to complete login.


### 3. Configure OAuth Provisioning URL

To enable request proxying:

- Go to:
  - View → Preferences → Advanced
- Set the **Oauth provisioning URL**.
  - Ensure `localhost` is included in the **hosts** list.
- Save changes.

### 4. Start the Local Proxy Server

Start the local IGV proxy server.

Refer to the [**Local Development**](#local-development) section for instructions on running the proxy.


### 5. Load Data in IGV

Once the proxy server is running, data can be loaded through the proxy.

- In IGV, go to:
  - File → Load from URL
  - Enter the file URL in the format
    - `http://localhost:8080/{gcs-bucket-name}/{cram-file-name}.cram`
  - Submit the request.

IGV will first load the CRAM index file. After it is loaded, navigate to a genomic region and zoom in to load the sequencing data.



## License

MIT (See [LICENSE](LICENSE) for details)
