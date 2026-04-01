import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

class ApiService {
  static String get baseUrl {
    const prodUrl = 'https://your-backend-url.com'; // Update with actual backend URL
    const localUrl = 'http://localhost:8000';
    
    String url;
    if (kDebugMode) {
      url = const String.fromEnvironment(
        'API_BASE_URL',
        defaultValue: localUrl
      );
    } else {
      url = const String.fromEnvironment(
        'API_BASE_URL',
        defaultValue: prodUrl
      );
    }
    return url;
  }
  
  static Future<Map<String, dynamic>> post(String endpoint, Map<String, dynamic> data) async {
    try {
      final url = '$baseUrl/$endpoint';
      final response = await http.post(
        Uri.parse(url),
        headers: {
          'Content-Type': 'application/json',
        },
        body: jsonEncode(data),
      );
      
      if (response.statusCode == 200 || response.statusCode == 201) {
        final body = utf8.decode(response.bodyBytes);
        if (body.isEmpty) {
          return {'success': true};
        }
        return jsonDecode(body) as Map<String, dynamic>;
      } else {
        final errorBody = utf8.decode(response.bodyBytes);
        throw Exception('Server error: ${response.statusCode} - $errorBody');
      }
    } catch (e) {
      if (e.toString().contains('Failed host lookup') || 
          e.toString().contains('Connection refused')) {
        throw Exception('Cannot connect to server. Make sure the backend is running at $baseUrl');
      }
      throw Exception('Request error: $e');
    }
  }

  static Future<Map<String, dynamic>> get(String endpoint) async {
    try {
      final response = await http.get(
        Uri.parse('$baseUrl/$endpoint'),
        headers: {
          'Content-Type': 'application/json',
        },
      );
      
      if (response.statusCode == 200) {
        final body = utf8.decode(response.bodyBytes);
        if (body.isEmpty) {
          return {};
        }
        return jsonDecode(body) as Map<String, dynamic>;
      } else {
        final errorBody = utf8.decode(response.bodyBytes);
        throw Exception('Server error: ${response.statusCode} - $errorBody');
      }
    } catch (e) {
      if (e.toString().contains('Failed host lookup') || 
          e.toString().contains('Connection refused')) {
        throw Exception('Cannot connect to server. Make sure the backend is running at $baseUrl');
      }
      throw Exception('Request error: $e');
    }
  }
  
  // Health check
  static Future<Map<String, dynamic>> healthCheck() async {
    return await get('health');
  }

  // Onboarding
  static Future<Map<String, dynamic>> saveOnboarding(Map<String, dynamic> onboardingData) async {
    return await post('onboarding', onboardingData);
  }

  // Learning modules
  static Future<Map<String, dynamic>> getLearningModules() async {
    return await get('learning/modules');
  }

  static Future<Map<String, dynamic>> getModuleContent(String moduleId, String userId) async {
    return await get('learning/modules/$moduleId?user_id=$userId');
  }

  // Portfolio simulation
  static Future<Map<String, dynamic>> simulatePortfolio(Map<String, dynamic> simulationData) async {
    return await post('portfolio/simulate', simulationData);
  }

  // Feed
  static Future<Map<String, dynamic>> getFeedItems(String userId, {String itemType = 'all', int limit = 10}) async {
    return await post('feed/items', {
      'user_id': userId,
      'item_type': itemType,
      'limit': limit,
    });
  }

  // Chat
  static Future<Map<String, dynamic>> chat(String userId, String message) async {
    return await post('chat', {
      'user_id': userId,
      'message': message,
    });
  }

  // User profile
  static Future<Map<String, dynamic>> getUserProfile(String userId) async {
    return await get('user/$userId');
  }

  static Future<Map<String, dynamic>> getUserProgress(String userId) async {
    return await get('user/$userId/progress');
  }
}

