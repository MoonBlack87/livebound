package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	maxBodyBytes = 1024
	// Umami rejects bot-like User-Agents for /api/send. This is a fixed,
	// synthetic browser-like UA and never contains client information.
	userAgent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

var uuidRE = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$`)

type config struct {
	ListenAddr    string
	UmamiEndpoint string
	WebsiteID     string
	Hostname      string
	EventName     string
}

type pingRequest struct {
	ID string `json:"id"`
}

type umamiEnvelope struct {
	Type    string       `json:"type"`
	Payload umamiPayload `json:"payload"`
}

type umamiPayload struct {
	Website  string         `json:"website"`
	Hostname string         `json:"hostname"`
	URL      string         `json:"url"`
	Name     string         `json:"name"`
	ID       string         `json:"id"`
	Data     map[string]any `json:"data"`
}

type umamiResponse struct {
	SessionID string `json:"sessionId"`
	VisitID   string `json:"visitId"`
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	client := &http.Client{Timeout: 5 * time.Second}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("POST /ping", pingHandler(cfg, client))

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      8 * time.Second,
		IdleTimeout:       30 * time.Second,
		ErrorLog:          log.New(io.Discard, "", 0),
	}

	if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintln(os.Stderr, "server stopped")
		os.Exit(1)
	}
}

func loadConfig() (config, error) {
	cfg := config{
		ListenAddr:    envOr("LISTEN_ADDR", ":8080"),
		UmamiEndpoint: strings.TrimSpace(os.Getenv("UMAMI_ENDPOINT")),
		WebsiteID:     strings.TrimSpace(os.Getenv("UMAMI_WEBSITE_ID")),
		Hostname:      envOr("UMAMI_HOSTNAME", "livebound"),
		EventName:     envOr("UMAMI_EVENT_NAME", "startup"),
	}
	if cfg.UmamiEndpoint == "" {
		return config{}, errors.New("UMAMI_ENDPOINT is required")
	}
	if cfg.WebsiteID == "" {
		return config{}, errors.New("UMAMI_WEBSITE_ID is required")
	}
	if !strings.HasPrefix(cfg.UmamiEndpoint, "https://") && !strings.HasPrefix(cfg.UmamiEndpoint, "http://") {
		return config{}, errors.New("UMAMI_ENDPOINT must start with http:// or https://")
	}
	return cfg, nil
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func pingHandler(cfg config, client *http.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if mediaType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])); mediaType != "application/json" {
			http.Error(w, "content-type must be application/json", http.StatusUnsupportedMediaType)
			return
		}

		r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
		decoder := json.NewDecoder(r.Body)
		decoder.DisallowUnknownFields()

		var input pingRequest
		if err := decoder.Decode(&input); err != nil {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}
		if err := ensureEOF(decoder); err != nil {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}

		input.ID = strings.ToLower(strings.TrimSpace(input.ID))
		if !uuidRE.MatchString(input.ID) {
			http.Error(w, "invalid uuid", http.StatusBadRequest)
			return
		}

		payload := umamiEnvelope{
			Type: "event",
			Payload: umamiPayload{
				Website:  cfg.WebsiteID,
				Hostname: cfg.Hostname,
				URL:      "/startup",
				Name:     cfg.EventName,
				ID:       input.ID,
				Data: map[string]any{
					"installation_id": input.ID,
				},
			},
		}

		body, err := json.Marshal(payload)
		if err != nil {
			http.Error(w, "upstream error", http.StatusBadGateway)
			return
		}

		upstreamReq, err := http.NewRequestWithContext(r.Context(), http.MethodPost, cfg.UmamiEndpoint, bytes.NewReader(body))
		if err != nil {
			http.Error(w, "upstream error", http.StatusBadGateway)
			return
		}
		upstreamReq.Header.Set("Content-Type", "application/json")
		upstreamReq.Header.Set("User-Agent", userAgent)

		resp, err := client.Do(upstreamReq)
		if err != nil {
			http.Error(w, "upstream unavailable", http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 8192))
		if err != nil {
			http.Error(w, "upstream error", http.StatusBadGateway)
			return
		}

		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			http.Error(w, "upstream rejected event", http.StatusBadGateway)
			return
		}

		// Umami may return a successful HTTP status for an ignored event.
		// A registered event response contains both sessionId and visitId.
		var registered umamiResponse
		if err := json.Unmarshal(responseBody, &registered); err != nil || registered.SessionID == "" || registered.VisitID == "" {
			http.Error(w, "upstream did not register event", http.StatusBadGateway)
			return
		}

		w.WriteHeader(http.StatusNoContent)
	}
}

func ensureEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); err == io.EOF {
		return nil
	} else if err != nil {
		return err
	}
	return errors.New("multiple json values")
}
