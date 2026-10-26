package com.arcanea.media;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Register before super.onCreate so the bridge exposes the plugin to the WebView.
        registerPlugin(LocalServerPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
