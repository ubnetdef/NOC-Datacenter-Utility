"use strict";

const query = new URLSearchParams(window.location.search);
const message = document.querySelector("#login-message");

if (query.get("failed") === "1") {
  message.textContent = "Sign-in failed. Check your account and management access.";
  message.hidden = false;
} else if (query.get("logged_out") === "1") {
  message.textContent = "You are signed out.";
  message.classList.add("login-success");
  message.hidden = false;
}

document.querySelector("#httpd_username").focus();
