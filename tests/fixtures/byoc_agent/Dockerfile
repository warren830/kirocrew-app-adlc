# The console's BYOC sample as a container: AgentCore Runtime runs linux/arm64 images (the console's CodeBuild
# project builds on ARM). The base comes from ECR Public's Docker Official Images mirror, which CodeBuild pulls
# without Docker Hub's anonymous rate limit.
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app
COPY main.py .
RUN useradd --create-home --uid 1000 agent
USER agent

# The Runtime's HTTP contract: POST /invocations and GET /ping on port 8080.
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
