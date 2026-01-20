import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuthProvider } from "react-oidc-context";

const cognitoAuthConfig = {
  // FIX: Pointing to Ohio (us-east-2) with your inferred Pool ID
  authority: "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_dNZFNYM23",
  
  client_id: "2hal73i1fbnr44n2mjkg8j2h8m",
  redirect_uri: "https://d269ewi535s8ar.cloudfront.net/index.html",
  response_type: "code",
  scope: "email openid phone",
};

const root = ReactDOM.createRoot(document.getElementById("root"));

root.render(
  <React.StrictMode>
    <AuthProvider {...cognitoAuthConfig}>
      <App />
    </AuthProvider>
  </React.StrictMode>
);