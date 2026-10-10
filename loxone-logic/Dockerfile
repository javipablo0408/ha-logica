FROM python:3.13-alpine
RUN pip install --no-cache-dir websockets
COPY engine /opt/loxone/engine
COPY catalogo_loxone.json catalogo_ha.json ui_catalogo.json ui_catalogo_ha.json /opt/loxone/
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
