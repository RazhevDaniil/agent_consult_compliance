FROM docker-internal.registry-ci.delta.sbrf.ru/ci04675739/ci04675739/python-3.12:9.7.0-min-se

RUN mkdir -p /app && chmod -R ugo+rwx /app

COPY ./dist/agent-ui ./app

CMD ["bin/sh", "-c", "./app/agent-ui"]

EXPOSE 8080