FROM gcr.io/{{ global.project }}/alpine:3.8
MAINTAINER hail-team@broadinstitute.org

RUN apk --no-cache add \
    bash \
    curl \
    python \
    docker \
    jq

RUN curl -sSL https://sdk.cloud.google.com | bash
ENV PATH $PATH:/root/google-cloud-sdk/bin

ADD fetch-image.sh ./
ENV PROJECT {{ global.project }}

CMD /bin/sh fetch-image.sh
