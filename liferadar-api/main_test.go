package main

import "testing"

func TestEnvBoolParsesTruthyValuesAndFallback(t *testing.T) {
	t.Setenv("LIFERADAR_TEST_BOOL", "true")
	if !envBool("LIFERADAR_TEST_BOOL", false) {
		t.Fatal("envBool(true) = false")
	}

	t.Setenv("LIFERADAR_TEST_BOOL", "0")
	if envBool("LIFERADAR_TEST_BOOL", true) {
		t.Fatal("envBool(0) = true")
	}

	t.Setenv("LIFERADAR_TEST_BOOL", "")
	if !envBool("LIFERADAR_TEST_BOOL", true) {
		t.Fatal("envBool(empty fallback true) = false")
	}
}
