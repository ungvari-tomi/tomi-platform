FROM ollama/ollama:0.40.17@sha256:ccc42bcd3d5fcf7ab0d532b300382154e1a4da56fc666bdb313b78ebdd912754
ENV HOME=/tmp OLLAMA_MODELS=/models OLLAMA_NO_CLOUD=1 OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=8192 \
    OLLAMA_KEEP_ALIVE=5m OLLAMA_MAX_QUEUE=1 OLLAMA_VULKAN=0
RUN groupadd --gid 10001 model && useradd --uid 10001 --gid 10001 --no-create-home model \
    && mkdir /models && chown 10001:10001 /models && chmod 0750 /models
COPY seed.sh /app/seed.sh
USER 10001:10001
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
