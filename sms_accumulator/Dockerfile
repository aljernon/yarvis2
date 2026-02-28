FROM golang:1.23-alpine AS builder
RUN apk add --no-cache gcc musl-dev
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY main.go .
RUN CGO_ENABLED=1 go build -o sms-accumulator .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=builder /build/sms-accumulator /usr/local/bin/
VOLUME /data
EXPOSE 8082
CMD ["sms-accumulator"]
