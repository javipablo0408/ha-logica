FROM python:3.13-alpine
RUN pip install --no-cache-dir websockets
COPY engine /opt/loxone/engine
COPY catalogo_loxone.json /opt/loxone/catalogo_loxone.json
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
