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

3. **Configure redis instance locally**:

    ```bash
    docker pull redis:7.2
    docker run --name igv-dsk-proxy-test -d -p 6379:6379 redis:7.2
    ```

    To visualize redis data - checkout [Redis insights](https://redis.io/docs/latest/operate/redisinsight/install/install-on-docker/)

4. **Configure the proxy**:

    ```bash
    export IGV_PROXY_CONFIG_PROJECT="<gcp-project-holding-the-igv-proxy-config-secret>"
    ```

    Required; the server refuses to start without it. It is the project holding the
    `igv-proxy-config` secret, which maps each permitted user to the buckets they may read. It is
    explicit rather than inferred from your credentials, which would silently point at your own
    gcloud project.

    You also need application default credentials with read access to that secret and to the
    buckets you want to serve: `gcloud auth application-default login`.

5. **Run the local server**:

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

## How access is granted

The proxy reads GCS with its own service account rather than forwarding the caller's token, so it
decides for itself who may read what. It resolves the caller's identity from their access token,
then checks that (email, bucket) pair against the `igv-proxy-config` secret.

That secret is built by cpg-infrastructure from the `igv-desktop-access` key in a dataset's
`members.yaml`. To grant someone access, add their email to that list — the proxy picks the change
up within `ACCESS_LIST_TTL_SECS` (5 minutes).

The proxy does not check which application the caller's access token was issued to: anyone in the
map can read the buckets they are listed against with any Google access token they hold, not only
one minted by IGV. Reaching the data outside IGV is not intended, but it is accepted — the access
map, not the calling application, is the control.

A user who holds personal IAM on a bucket does not need the proxy at all: IGV Desktop has native
Google/GCS support. The proxy exists for people who deliberately have no personal IAM, so that
"IGV viewing access" can be a narrower grant than "bucket read access".

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
