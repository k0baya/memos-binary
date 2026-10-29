// Built only for CI using the same Android NDK and netcgo resolver as Memos.
package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

func main() {
	addresses, err := net.LookupHost("example.com")
	if err != nil || len(addresses) == 0 {
		fmt.Fprintln(os.Stderr, "Android system DNS failed:", err)
		os.Exit(1)
	}
	client := &http.Client{Timeout: 20 * time.Second}
	response, err := client.Get("https://example.com")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Android system CA/HTTPS failed:", err)
		os.Exit(1)
	}
	defer response.Body.Close()
	if _, err := io.Copy(io.Discard, response.Body); err != nil || response.StatusCode >= 400 {
		fmt.Fprintln(os.Stderr, "Android HTTPS response failed:", response.Status, err)
		os.Exit(1)
	}
	fmt.Println("Android Bionic system DNS and CA/HTTPS OK")
}
