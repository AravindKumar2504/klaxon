package main

// Settings from the environment, read as src/klaxon/patient/services/common.py and receiver_app.py read them.

import (
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"
)

// secretVersions are the versions of the webhook secret, each held in WEBHOOK_SECRET_<VERSION>.
var secretVersions = []string{"v1", "v2"}

// secretsFromEnv returns the secrets of the versions WEBHOOK_SECRET_VERSION names (v1 when it is unset or empty;
// comma-separated during a rotation: v1,v2). The provider signs with v1. A version it does not know, or one whose
// secret is missing, stops the service: it never runs with no secret. A deploy names only the version, never a
// secret.
func secretsFromEnv(getenv func(string) string) ([][]byte, error) {
	setting := getenv("WEBHOOK_SECRET_VERSION")
	if setting == "" {
		setting = "v1"
	}
	var secrets [][]byte
	for _, item := range strings.Split(setting, ",") {
		version := strings.TrimSpace(item)
		if version == "" {
			continue
		}
		if !slices.Contains(secretVersions, version) {
			return nil, fmt.Errorf("WEBHOOK_SECRET_VERSION names '%s'; the versions are %s", version,
				strings.Join(secretVersions, ", "))
		}
		name := "WEBHOOK_SECRET_" + strings.ToUpper(version)
		secret := getenv(name)
		if secret == "" {
			return nil, fmt.Errorf("missing required setting %s", name)
		}
		secrets = append(secrets, []byte(secret))
	}
	if len(secrets) == 0 {
		return nil, errors.New("WEBHOOK_SECRET_VERSION names no version")
	}
	return secrets, nil
}

// settingText is a setting's value, or its default when it is unset. Set but empty is missing, as in Python.
func settingText(lookup func(string) (string, bool), name, fallback string) (string, error) {
	value, isSet := lookup(name)
	if !isSet {
		value = fallback
	}
	if value == "" {
		return "", fmt.Errorf("missing required setting %s", name)
	}
	return value, nil
}

// settingNumber is a whole-number setting, or its default when it is unset or empty.
func settingNumber(lookup func(string) (string, bool), name string, fallback int) (int, error) {
	value, _ := lookup(name)
	if value == "" {
		return fallback, nil
	}
	number, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil {
		return 0, fmt.Errorf("setting %s is not a whole number: %q", name, value)
	}
	return number, nil
}
