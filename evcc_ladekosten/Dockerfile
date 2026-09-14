FROM ghcr.io/home-assistant/base:latest

LABEL \
    org.opencontainers.image.title="evcc Charging-Protocol" \
    org.opencontainers.image.description="A Home Assistant add-on that retrieves charging records for an evcc instance via its REST API and uses them to generate a PDF receipt for claiming reimbursement of company car charging costs from the employer." \
    org.opencontainers.image.licenses="MIT"

# Python + pip + DejaVu Sans (for higher-quality PDF typography)
# on the Alpine-based HA base image
RUN apk add --no-cache python3 py3-pip font-dejavu

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
