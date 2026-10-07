import React from "react";
import logo from "../assets/logo.png";

export default function Header({ onNavigate }) {
  return (
    <div className="header">
      <img src={logo} />
      <div className="nav-item" onClick={() => onNavigate("/")}>Home</div>
      <div className="nav-item" onClick={() => onNavigate("/apply")}>Apply</div>
      <a href="/help">click here</a>
    </div>
  );
}
