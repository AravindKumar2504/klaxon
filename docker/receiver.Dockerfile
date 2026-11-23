# The webhook receiver, in Go (week 8, item 2): built in the official golang image, run from an empty one.
#
# golang:1.27.3-alpine, pinned by digest: Go 1.27.3 was released 2026-10-19; docker-library/repo-info recorded this
# image index on 2026-10-27, its linux/amd64 and linux/arm64 images built 2026-10-21. The Makefile's GO_IMAGE is the
# same one: make test-go runs the tests in it, here and in CI.
FROM golang:1.27.3-alpine@sha256:d200d2dd76b03a99b52651fd1325aff7dcd35422c35c01bac56571b0507f3deb AS build

# No C: a static binary that needs nothing from the image it runs in.
ENV CGO_ENABLED=0 GOFLAGS=-mod=readonly
WORKDIR /src

# The modules first, so editing our code does not download them again. go.sum pins each one's hash.
COPY go/receiver/go.mod go/receiver/go.sum ./
RUN go mod download && go mod verify

COPY go/receiver/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/receiver .

# scratch: no shell, no package manager, nothing but the binary. It checks its own health (/receiver -health).
FROM scratch
COPY --from=build /out/receiver /receiver
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/receiver"]
