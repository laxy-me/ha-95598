ARG BUILD_FROM=ghcr.io/renxiaoyaoo/ha-95598:latest
FROM ${BUILD_FROM}

# Overlay our patched source files on top of the base image.
# Base image ships an older copy of captcha_solver/ and scripts/
# from upstream renxiaoyaoo/ha-95598; we replace those with the
# fork's local copies (PID 1 fix, LLM vision solver, captcha
# detection relaxation, etc).
COPY _overlay/captcha_solver/ /app/captcha_solver/
COPY _overlay/scripts/ /app/scripts/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
