FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/livebound-ping ./main.go

FROM alpine:3.22
RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 livebound \
    && adduser -S -D -H -u 10001 -G livebound livebound
COPY --from=build /out/livebound-ping /usr/local/bin/livebound-ping
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/livebound-ping"]
