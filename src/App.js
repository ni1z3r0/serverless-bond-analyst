import React from "react";
import { useAuth } from "react-oidc-context";

function App() {
  const auth = useAuth();

  const signOutRedirect = () => {
    const clientId = "2hal73i1fbnr44n2mjkg8j2h8m"; // Your App Client ID
    const logoutUri = "https://d269ewi535s8ar.cloudfront.net/index.html"; // Your CloudFront URL
    
    // FIX: Updated to match your actual Cognito Domain from the console
    const cognitoDomain = "https://us-east-2dnzfnym23.auth.us-east-2.amazoncognito.com";
    
    // Redirect to AWS Logout Endpoint
    window.location.href = `${cognitoDomain}/logout?client_id=${clientId}&logout_uri=${encodeURIComponent(logoutUri)}`;
  };

  if (auth.isLoading) {
    return <div>Loading authentication...</div>;
  }

  if (auth.error) {
    return <div>Encountered error: {auth.error.message}</div>;
  }

  if (auth.isAuthenticated) {
    return (
      <div style={{padding: "20px", fontFamily: "system-ui, sans-serif"}}>
        <h1>Welcome back!</h1>
        <p>User: <strong>{auth.user?.profile.email}</strong></p>
        
        <div style={{background: "#f0f0f0", padding: "15px", margin: "20px 0", borderRadius: "8px", overflow: "auto"}}>
            <p><strong>ID Token:</strong> {auth.user?.id_token.substring(0, 20)}...</p>
            <p><strong>Access Token:</strong> {auth.user?.access_token.substring(0, 20)}...</p>
        </div>

        <button 
            onClick={signOutRedirect}
            style={{padding: "10px 20px", cursor: "pointer", background: "#e74c3c", color: "white", border: "none", borderRadius: "4px", fontSize: "16px"}}
        >
            Sign out
        </button>
        
        {/* This is where you will eventually paste your Dashboard HTML */}
      </div>
    );
  }

  return (
    <div style={{display: "flex", height: "100vh", justifyContent: "center", alignItems: "center", flexDirection: "column", fontFamily: "system-ui, sans-serif"}}>
      <h1 style={{color: "#2c3e50"}}>Bond Analyst Pro</h1>
      <button 
        onClick={() => auth.signinRedirect()}
        style={{padding: "15px 30px", fontSize: "16px", cursor: "pointer", background: "#2980b9", color: "white", border: "none", borderRadius: "4px"}}
      >
        Log in via AWS Cognito
      </button>
    </div>
  );
}

export default App;